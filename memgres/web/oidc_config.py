"""Sign-in providers, read from the file ``MEMGRES_OIDC_CONFIG`` points at.

A TOML file, one table per provider — each configured on its own, because a
deployment's providers differ in who they vouch for. An employee directory can
be trusted to create accounts; Google cannot::

    [providers.corp]
    label = "Company SSO"
    issuer = "https://id.example.com"
    client_id = "memgres"
    client_secret_file = "/run/secrets/corp-oidc"   # omit for a public client (PKCE only)
    allowed_email_domains = ["example.com"]
    on_email_match = "link"        # link | pending | deny
    on_no_match = "create"         # create | pending | deny

    [[providers.corp.require]]     # every condition must hold, or sign-in is refused
    claim = "urn:zitadel:iam:org:project:roles"
    has = "memgres"

The deployment mode only supplies DEFAULTS: in ``managed`` a provider neither
links by email nor creates accounts unless its table says so; in ``open`` it
does both. Relaxing ``managed`` is a deliberate line in this file, per provider.

Secrets are never in the file itself — only the path of a file holding one.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

ACTIONS_MATCH = ("link", "pending", "deny")
ACTIONS_NO_MATCH = ("create", "pending", "deny")
TOKEN_AUTH = ("client_secret_basic", "client_secret_post", "none")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class OIDCConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Condition:
    claim: Tuple[str, ...]     # a path of keys into the claims
    has: Any = None            # list contains / object has key / string equals
    equals: Any = None         # exact value

    def holds(self, claims: dict) -> bool:
        cur: Any = claims
        for key in self.claim:
            if not isinstance(cur, dict) or key not in cur:
                return False
            cur = cur[key]
        if self.equals is not None:
            return cur == self.equals
        want = self.has
        if isinstance(cur, dict):
            return want in cur
        if isinstance(cur, (list, tuple)):
            return want in cur
        if isinstance(cur, str):
            # space-separated lists (e.g. `scope`, `amr` from some providers)
            return cur == want or want in cur.split()
        return False


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    issuer: str
    client_id: str
    client_secret: Optional[str] = field(default=None, repr=False)
    scopes: Tuple[str, ...] = ("openid", "email", "profile")
    token_auth: str = "client_secret_basic"
    require: Tuple[Condition, ...] = ()
    allowed_email_domains: Tuple[str, ...] = ()
    on_email_match: str = "pending"
    on_no_match: str = "deny"
    sync_on_login: bool = False
    hint: str = ""
    allow_http: bool = False   # plain-http issuer, for a local test provider only


def _read_secret(path: str, pid: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            secret = f.read().strip()
    except OSError as e:
        raise OIDCConfigError(f"provider {pid!r}: cannot read client_secret_file {path!r}: {e}")
    if not secret:
        raise OIDCConfigError(f"provider {pid!r}: client_secret_file {path!r} is empty")
    return secret


def parse(data: dict, *, key_mode: str) -> Dict[str, Provider]:
    """Validate a decoded config. Everything wrong is refused at startup — a
    provider that half-works is a sign-in door nobody can reason about."""
    default_match = "link" if key_mode == "open" else "pending"
    default_no_match = "create" if key_mode == "open" else "deny"
    raw = data.get("providers")
    if not isinstance(raw, dict) or not raw:
        raise OIDCConfigError("the OIDC config has no [providers.<id>] tables")
    unknown_top = set(data) - {"providers"}
    if unknown_top:
        raise OIDCConfigError(f"unknown top-level keys: {', '.join(sorted(unknown_top))}")

    known = {"label", "issuer", "client_id", "client_secret_file", "scopes", "token_auth",
             "require", "allowed_email_domains", "on_email_match", "on_no_match",
             "sync_on_login", "hint", "allow_http"}
    out: Dict[str, Provider] = {}
    for pid, p in raw.items():
        if not _ID_RE.match(pid):
            raise OIDCConfigError(f"provider id {pid!r}: lowercase letters, digits, '-' and '_' only")
        if not isinstance(p, dict):
            raise OIDCConfigError(f"provider {pid!r} must be a table")
        extra = set(p) - known
        if extra:
            # a typo like `on_no_mach` must not silently fall back to a default
            raise OIDCConfigError(f"provider {pid!r}: unknown keys {', '.join(sorted(extra))}")
        for req in ("issuer", "client_id"):
            if not isinstance(p.get(req), str) or not p[req].strip():
                raise OIDCConfigError(f"provider {pid!r}: {req} is required")
        # exactly as the provider publishes it — a trailing slash included. The
        # id_token's `iss` is compared to this character for character.
        issuer = p["issuer"].strip()
        allow_http = bool(p.get("allow_http", False))
        if not issuer.startswith("https://") and not (allow_http and issuer.startswith("http://")):
            raise OIDCConfigError(f"provider {pid!r}: issuer must be https:// (allow_http is for local testing)")

        token_auth = p.get("token_auth")
        secret = None
        if p.get("client_secret_file"):
            secret = _read_secret(str(p["client_secret_file"]), pid)
            token_auth = token_auth or "client_secret_basic"
        else:
            token_auth = token_auth or "none"
        if token_auth not in TOKEN_AUTH:
            raise OIDCConfigError(f"provider {pid!r}: token_auth must be one of {', '.join(TOKEN_AUTH)}")
        if token_auth != "none" and secret is None:
            raise OIDCConfigError(f"provider {pid!r}: token_auth {token_auth} needs client_secret_file")

        scopes = p.get("scopes", ["openid", "email", "profile"])
        if not isinstance(scopes, list) or "openid" not in scopes:
            raise OIDCConfigError(f"provider {pid!r}: scopes must be a list including 'openid'")

        conds: List[Condition] = []
        for i, c in enumerate(p.get("require", []) or []):
            if not isinstance(c, dict) or "claim" not in c or (("has" in c) == ("equals" in c)):
                raise OIDCConfigError(f"provider {pid!r}: require[{i}] needs `claim` and exactly one of `has` / `equals`")
            path = c["claim"]
            path = tuple(path) if isinstance(path, list) else (str(path),)
            if not path or not all(isinstance(k, str) and k for k in path):
                raise OIDCConfigError(f"provider {pid!r}: require[{i}].claim must be a key or a list of keys")
            conds.append(Condition(claim=path, has=c.get("has"), equals=c.get("equals")))

        domains = p.get("allowed_email_domains", [])
        if not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains):
            raise OIDCConfigError(f"provider {pid!r}: allowed_email_domains must be a list of domains")

        on_match = p.get("on_email_match", default_match)
        on_no = p.get("on_no_match", default_no_match)
        if on_match not in ACTIONS_MATCH:
            raise OIDCConfigError(f"provider {pid!r}: on_email_match must be one of {', '.join(ACTIONS_MATCH)}")
        if on_no not in ACTIONS_NO_MATCH:
            raise OIDCConfigError(f"provider {pid!r}: on_no_match must be one of {', '.join(ACTIONS_NO_MATCH)}")

        out[pid] = Provider(
            id=pid, label=str(p.get("label") or pid), issuer=issuer, client_id=p["client_id"].strip(),
            client_secret=secret, scopes=tuple(scopes), token_auth=token_auth, require=tuple(conds),
            allowed_email_domains=tuple(d.lower().lstrip("@") for d in domains),
            on_email_match=on_match, on_no_match=on_no, sync_on_login=bool(p.get("sync_on_login", False)),
            hint=str(p.get("hint", "")), allow_http=allow_http)
    return out


def load(path: str, *, key_mode: str) -> Dict[str, Provider]:
    if not path:
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:          # Python 3.10
        import tomli as tomllib          # type: ignore[no-redef]
    if not os.path.isfile(path):
        raise OIDCConfigError(f"MEMGRES_OIDC_CONFIG {path!r} does not exist")
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise OIDCConfigError(f"MEMGRES_OIDC_CONFIG {path!r}: {e}")
    return parse(data, key_mode=key_mode)
