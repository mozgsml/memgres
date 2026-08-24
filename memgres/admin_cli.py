"""``memgres-grant-superadmin`` — promote a user to the superadmin service role.

The break-glass path for creating a superadmin when bootstrap seeded only a
``user_manager`` (or none), and the way out of a lockout (the last superadmin was
revoked). Like Django's ``createsuperuser``, it talks **directly to the database**
(``MEMGRES_DATABASE_URL``) — so the gate is host/DB access, not a network token.

    memgres-grant-superadmin --list                 # show users + roles
    memgres-grant-superadmin --user <uuid>
    memgres-grant-superadmin --token-label <label>  # resolve the user by a token label
    memgres-grant-superadmin --revoke --user <uuid> # demote (anti-lockout applies)

A raw ``UPDATE`` is the last-ditch fallback; prefer this so the change is
validated (real user, anti-lockout) rather than silently wrong.
"""

from __future__ import annotations

import argparse
import sys

from . import identity
from .config import load


def _resolve_user(conn, args) -> str:
    """Return the target user id from --user or --token-label, or exit with a
    clear message."""
    if args.user:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_user WHERE id=%s", (args.user,))
            if cur.fetchone() is None:
                raise SystemExit(f"no such user: {args.user}")
        return args.user
    # by token label — must be unambiguous
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT user_id FROM token WHERE label=%s "
                    "AND revoked_at IS NULL", (args.token_label,))
        rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no active token labelled {args.token_label!r}")
    if len(rows) > 1:
        raise SystemExit(
            f"token label {args.token_label!r} maps to {len(rows)} users — "
            "use --user <uuid> instead")
    return str(rows[0][0])


def _list_users(conn) -> None:
    """Print the directory. The query itself lives in `identity.list_users`, so
    the CLI and both servers cannot disagree about what a user record is."""
    rows = identity.list_users(conn)
    if not rows:
        print("(no users yet)")
        return
    # Authority first, then oldest — the operator is here to find an admin.
    rows.sort(key=lambda u: (u["role"] == "user", u["role"] != "superadmin"))
    print(f"{'id':36}  {'role':12}  name / description")
    for u in rows:
        label = u["name"] or u["description"] or ""
        print(f"{u['id']:36}  {u['role']:12}  {label}")


def main(argv=None) -> None:  # pragma: no cover - thin entrypoint
    import logging

    import psycopg

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="memgres-grant-superadmin",
        description="Promote (or demote) a user's superadmin service role.")
    p.add_argument("--list", action="store_true",
                   help="list users and their roles, then exit")
    p.add_argument("--user", metavar="UUID", help="target user id")
    p.add_argument("--token-label", metavar="LABEL",
                   help="resolve the target user by one of its token labels")
    p.add_argument("--revoke", action="store_true",
                   help="demote the user out of superadmin (anti-lockout applies)")
    p.add_argument("--demote-to", default="user", choices=("user", "user_manager"),
                   help="role to demote to with --revoke (default: user)")
    args = p.parse_args(argv)

    cfg = load()
    with psycopg.connect(cfg.database_url or "") as conn:
        conn.autocommit = True
        try:
            if args.list:
                _list_users(conn)
                return
            if not args.user and not args.token_label:
                p.error("give --user or --token-label (or --list)")
            uid = _resolve_user(conn, args)
            if args.revoke:
                identity.revoke_superadmin(conn, uid, demote_to=args.demote_to)
                print(f"revoked superadmin from {uid} (now {args.demote_to})")
            else:
                identity.grant_superadmin(conn, uid)
                print(f"granted superadmin to {uid}")
        except identity.AuthError as e:          # anti-lockout
            raise SystemExit(f"refused: {e}")
        except psycopg.errors.UndefinedColumn:
            raise SystemExit(
                "this database has no service-role column yet — start "
                "memgres-server/-mcp once to migrate it, then retry.")


if __name__ == "__main__":  # pragma: no cover
    main()
