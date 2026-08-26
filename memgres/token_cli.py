"""``memgres-provision`` — create a user, give it a namespace, mint its token,
**without the secret ever being printed into somebody's chat log**.

The problem this exists for: provisioning is increasingly done BY an agent, over
MCP, and a minted secret in a tool result is a secret in a transcript — logged,
summarized, replayed into a model's context. The MCP and REST doors solve that
with ``MEMGRES_TOKEN_SINK`` (the secret goes to a file, the reply carries only
its path). This CLI is the other half: the operator's own shell, where the
secret can simply be written to a file and never rendered at all.

Like ``memgres-grant-superadmin`` it talks **directly to the database**
(``MEMGRES_DATABASE_URL``), so the gate is host/DB access rather than a network
token — there is no admin token to hold, and nothing to leak in transit.

    # a whole new person: user + their own namespace + a token
    memgres-provision --name ivan --full-name "Иван Петров" --space ivan

    # just another token for someone who exists (rotation, a second device)
    memgres-provision --user <uuid> --label laptop --expires-days 90

    # where the secret went
    #   --out PATH   → that file (0600)
    #   otherwise    → MEMGRES_TOKEN_SINK/<token-id>.token, if the sink is set
    #   otherwise    → stdout, and it says so on stderr

Everything except the secret is printed: the user id, the namespace id, the
token id. Those are what you paste back into the conversation.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import identity
from .config import load


def _fail(msg: str) -> None:
    raise SystemExit(f"refused: {msg}")


def _list_users(conn) -> None:
    rows = identity.list_users(conn)
    if not rows:
        print("(no users yet)")
        return
    print(f"{'id':36}  {'role':12}  name / description")
    for u in rows:
        print(f"{u['id']:36}  {u['role']:12}  {u['name'] or u['description'] or ''}")


def _resolve_user(conn, args) -> str:
    """The user the token is for: an existing one, or one created right here."""
    if args.user:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_user WHERE id=%s", (args.user,))
            if cur.fetchone() is None:
                _fail(f"no such user: {args.user}")
        return args.user
    uid = identity.create_user(
        conn, args.name, args.description, role=args.role,
        can_create_namespace=args.can_create_namespace,
        email=args.email, full_name=args.full_name,
        department=args.department, position=args.position)
    print(f"user      {uid}  ({args.name or args.full_name or 'unnamed'})")
    return uid


def _namespace(conn, args, uid: str) -> str | None:
    """The namespace the token is scoped to, creating it when asked.

    A fresh user owns nothing by construction (see `identity.create_user`), so a
    token minted for one without this reaches an empty deployment and every call
    it makes fails on "no namespace" — which reads like a broken server rather
    than an unfinished provisioning.
    """
    if args.space_id:
        return args.space_id
    if not args.space:
        return None
    nsid = identity.create_namespace(conn, uid, args.space,
                                     description=args.space_description,
                                     instruction=args.space_instruction)
    print(f"namespace {nsid}  ({args.space})")
    return nsid


def _deliver(secret: str, tid: str, out: str | None, sink: str,
             *, kind: str = "token") -> None:
    """Put the secret where the operator asked, and say only where that was.

    `kind` is what to call it in the output — an enrollment key travels through
    exactly this path, because it is a bearer credential for as long as it lives
    and an operator who set a sink meant all of them.
    """
    if out:
        identity.write_private(out, secret + "\n")
        print(f"{kind:9} {tid}  → {out}")
        return
    if sink:
        print(f"{kind:9} {tid}  → {identity.stash_secret(sink, tid, secret)}")
        return
    print(f"memgres: no --out and no MEMGRES_TOKEN_SINK — printing the {kind} to "
          "stdout, where your shell history and your terminal scrollback will "
          "keep it.", file=sys.stderr)
    print(f"{kind:9} {tid}")
    print(secret)


def main(argv=None) -> None:  # pragma: no cover - thin entrypoint
    import psycopg

    p = argparse.ArgumentParser(
        prog="memgres-provision",
        description="Create a user + namespace + token without printing the "
                    "secret into a transcript.")
    p.add_argument("--list", action="store_true", help="list users, then exit")
    who = p.add_argument_group("who the token is for")
    who.add_argument("--user", metavar="UUID",
                     help="an existing user (omit to create one)")
    who.add_argument("--name", default="", help="short handle for a new user")
    who.add_argument("--description", default="")
    who.add_argument("--full-name", default=None)
    who.add_argument("--email", default=None)
    who.add_argument("--department", default=None)
    who.add_argument("--position", default=None)
    who.add_argument("--role", default="user",
                     choices=("user", "user_manager", "superadmin"),
                     help="service role for a NEW user (default: user)")
    who.add_argument("--can-create-namespace", action="store_true",
                     help="let the new user make its own namespaces")
    sp = p.add_argument_group("namespace")
    sp.add_argument("--space", metavar="NAME",
                    help="create (or reuse) this namespace, owned by the user")
    sp.add_argument("--space-id", metavar="UUID",
                    help="scope the token to an existing namespace instead")
    sp.add_argument("--space-description", default="")
    sp.add_argument("--space-instruction", default="",
                    help="routing text agents read when choosing a namespace")
    tk = p.add_argument_group("token")
    tk.add_argument("--permission", default="write",
                    choices=("read", "write", "admin"), help="the token's ceiling")
    tk.add_argument("--label", default="", help="what this token is for")
    tk.add_argument("--expires-days", type=int, default=None)
    tk.add_argument("--no-token", action="store_true",
                    help="provision the user/namespace only, mint nothing")
    tk.add_argument("--enroll", action="store_true",
                    help="issue a one-time ENROLLMENT KEY instead of a token: "
                         "the person generates their own token and binds it, so "
                         "no secret is created here at all")
    tk.add_argument("--enroll-minutes", type=int, default=None,
                    metavar="N", help="how long the key lives (default 30)")
    tk.add_argument("--out", metavar="PATH",
                    help="write the secret here (0600) instead of stdout")
    args = p.parse_args(argv)

    cfg = load()
    with psycopg.connect(cfg.database_url or "") as conn:
        try:
            if args.list:
                _list_users(conn)
                return
            if not args.user and not (args.name or args.full_name):
                p.error("give --user <uuid>, or --name/--full-name to create one")
            with conn.transaction():
                uid = _resolve_user(conn, args)
                nsid = _namespace(conn, args, uid)
                if args.no_token:
                    return
                if args.enroll:
                    out = identity.create_enrollment(
                        conn, uid, namespace_id=nsid,
                        permission=args.permission, label=args.label,
                        **({} if args.enroll_minutes is None
                           else {"expires_minutes": args.enroll_minutes}))
                    # The key is printed: unlike a token it is single-use and
                    # dies within the hour, and it has to reach a human somehow.
                    print(f"key       {out['id']}  expires "
                          f"{out['expires_at']:%Y-%m-%d %H:%M %Z}")
                    # Through the SAME delivery as a token. It used to print the
                    # key regardless — ignoring --out, ignoring the sink, and
                    # without even the stderr warning the token path emits —
                    # while an operator who configured a sink believed every
                    # credential was being diverted to it.
                    _deliver(out["key"], out["id"], args.out, cfg.token_sink,
                             kind="key")
                    print("\nGive that key to its owner. They run:\n"
                          "  python3 -c \"import secrets; print('mgk_' + "
                          "secrets.token_urlsafe(32))\"\n"
                          "put the result in their client's memgres config, and "
                          "call memory_enroll with the key.")
                    return
                expires_at = None
                if args.expires_days:
                    import datetime as dt
                    expires_at = (dt.datetime.now(dt.timezone.utc)
                                  + dt.timedelta(days=args.expires_days))
                secret, tid = identity.issue_token(
                    conn, uid, namespace_id=nsid, permission=args.permission,
                    label=args.label, expires_at=expires_at)
            # Outside the transaction: the token is real before its secret is
            # written anywhere, so a failed write leaves a revocable token rather
            # than a file naming one that never existed.
            _deliver(secret, tid, args.out, cfg.token_sink)
        except (identity.AuthError, ValueError) as e:
            _fail(str(e))
        except psycopg.errors.UndefinedTable:
            raise SystemExit(
                "this database has no memgres schema yet — start memgres-server "
                "or memgres-mcp once to migrate it, then retry.")


if __name__ == "__main__":  # pragma: no cover
    main()
