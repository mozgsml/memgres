"""OpenID Connect, the protocol half: discovery, the authorization URL, the code
exchange, and verifying what comes back. No database, no decisions about who
gets in — that is :mod:`memgres.web.admission`.

Authorization Code flow with PKCE (S256), a random ``state`` and ``nonce``.
The ``id_token`` is verified locally against the provider's published keys:
signature (asymmetric algorithms only — never ``none``, never a shared-secret
HMAC), ``iss`` character for character, ``aud`` containing our client id (and
``azp`` when there are several audiences), ``exp``/``iat`` with a minute of
clock skew, and the ``nonce`` this flow sent.

Network calls go through :func:`http_json` so tests can stand in a provider
without a socket.
"""

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .oidc_config import Provider

ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                "ES256", "ES384", "ES512", "EdDSA")
CACHE_S = 3600
LEEWAY_S = 60
TIMEOUT_S = 10


class OIDCError(Exception):
    """The provider or its answer could not be trusted. The message is for the
    server log; the person signing in gets a generic failure."""


def http_json(method: str, url: str, *, data: Optional[dict] = None,
              headers: Optional[dict] = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/x-www-form-urlencoded"} if body else {}),
        **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            raw = r.read(1_000_000)
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", "replace")
        raise OIDCError(f"{method} {url}: HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OIDCError(f"{method} {url}: {e}") from None
    try:
        out = json.loads(raw)
    except ValueError:
        raise OIDCError(f"{method} {url}: not JSON") from None
    if not isinstance(out, dict):
        raise OIDCError(f"{method} {url}: expected a JSON object")
    return out


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class Client:
    """One per provider; caches discovery and keys for an hour, and refetches
    keys once when a token names a key it has not seen (a rotation)."""

    def __init__(self, provider: Provider, fetch=None):
        self.p = provider
        self._fetch = fetch or http_json
        self._lock = threading.Lock()
        self._meta: Optional[dict] = None
        self._meta_at = 0.0
        self._jwks = None
        self._jwks_at = 0.0

    # ─── discovery & keys ───────────────────────────────────────────────────
    def metadata(self) -> dict:
        with self._lock:
            if self._meta and time.monotonic() - self._meta_at < CACHE_S:
                return self._meta
        url = self.p.issuer.rstrip("/") + "/.well-known/openid-configuration"
        meta = self._fetch("GET", url)
        if meta.get("issuer") != self.p.issuer:
            # OIDC Discovery §4.3: a mismatch here is how a mix-up attack looks
            raise OIDCError(f"{self.p.id}: discovery says issuer {meta.get('issuer')!r}, "
                            f"config says {self.p.issuer!r}")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(meta.get(key), str):
                raise OIDCError(f"{self.p.id}: discovery document has no {key}")
            if not self._secure(meta[key]):
                raise OIDCError(f"{self.p.id}: {key} is not https")
        with self._lock:
            self._meta, self._meta_at = meta, time.monotonic()
        return meta

    def _secure(self, url: str) -> bool:
        return url.startswith("https://") or (self.p.allow_http and url.startswith("http://"))

    def _keys(self, force: bool = False):
        import jwt
        with self._lock:
            fresh = self._jwks is not None and time.monotonic() - self._jwks_at < CACHE_S
            # a forced refetch is still limited to once a minute, so a stream of
            # tokens naming unknown keys cannot turn us into a request amplifier
            if fresh and (not force or time.monotonic() - self._jwks_at < 60):
                return self._jwks
        data = self._fetch("GET", self.metadata()["jwks_uri"])
        try:
            keyset = jwt.PyJWKSet.from_dict(data)
        except jwt.PyJWTError as e:
            raise OIDCError(f"{self.p.id}: unusable JWKS: {e}") from None
        with self._lock:
            self._jwks, self._jwks_at = keyset, time.monotonic()
        return keyset

    # ─── the flow ───────────────────────────────────────────────────────────
    def authorize_url(self, *, redirect_uri: str, state: str, nonce: str, challenge: str,
                      login_hint: Optional[str] = None, prompt: Optional[str] = None) -> str:
        meta = self.metadata()
        q = {"response_type": "code", "client_id": self.p.client_id, "redirect_uri": redirect_uri,
             "scope": " ".join(self.p.scopes), "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        if login_hint:
            q["login_hint"] = login_hint
        if prompt:
            q["prompt"] = prompt
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return meta["authorization_endpoint"] + sep + urllib.parse.urlencode(q)

    def exchange(self, *, code: str, redirect_uri: str, verifier: str) -> dict:
        meta = self.metadata()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier}
        headers = {}
        if self.p.token_auth == "client_secret_basic":
            cred = f"{urllib.parse.quote(self.p.client_id, safe='')}:{urllib.parse.quote(self.p.client_secret or '', safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        else:
            form["client_id"] = self.p.client_id
            if self.p.token_auth == "client_secret_post":
                form["client_secret"] = self.p.client_secret or ""
        tokens = self._fetch("POST", meta["token_endpoint"], data=form, headers=headers)
        if not isinstance(tokens.get("id_token"), str):
            raise OIDCError(f"{self.p.id}: token response has no id_token")
        return tokens

    def verify_id_token(self, id_token: str, *, nonce: str) -> dict:
        import jwt
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as e:
            raise OIDCError(f"{self.p.id}: malformed id_token: {e}") from None
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise OIDCError(f"{self.p.id}: id_token alg {alg!r} is not accepted")
        key = self._key_for(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token, key.key, algorithms=[alg], audience=self.p.client_id,
                issuer=self.p.issuer, leeway=LEEWAY_S,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]})
        except jwt.PyJWTError as e:
            raise OIDCError(f"{self.p.id}: id_token rejected: {e}") from None
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.p.client_id:
            raise OIDCError(f"{self.p.id}: id_token has several audiences and azp is not ours")
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OIDCError(f"{self.p.id}: id_token nonce does not match this sign-in")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise OIDCError(f"{self.p.id}: id_token has no subject")
        return claims

    def _key_for(self, kid: Optional[str]):
        for force in (False, True):
            keyset = self._keys(force=force)
            if kid is None:
                keys = list(keyset)
                if len(keys) == 1:
                    return keys[0]
                raise OIDCError(f"{self.p.id}: id_token names no key and the JWKS has several")
            try:
                return keyset[kid]
            except KeyError:
                continue
        raise OIDCError(f"{self.p.id}: no published key {kid!r}")

    def userinfo(self, access_token: Optional[str], sub: str) -> dict:
        """Claims from the userinfo endpoint, if the provider has one. Some put
        roles or group membership only there. A different `sub` is refused
        (OIDC Core §5.3.2): it would be someone else's claims."""
        endpoint = self.metadata().get("userinfo_endpoint")
        if not access_token or not isinstance(endpoint, str) or not self._secure(endpoint):
            return {}
        info = self._fetch("GET", endpoint, headers={"Authorization": f"Bearer {access_token}"})
        if info.get("sub") != sub:
            raise OIDCError(f"{self.p.id}: userinfo sub does not match the id_token")
        return info


def merged_claims(id_claims: dict, info: dict) -> dict:
    """Userinfo fills in; the id_token wins where both speak. The id_token is
    the signed statement, so nothing security-relevant is taken over from the
    unsigned side when the signed side has it."""
    out = dict(info)
    out.update(id_claims)
    # An address and the word that it is verified travel together. If the
    # id_token names an email but says nothing about it, userinfo's flag counts
    # only when userinfo names the same address — otherwise it vouches for a
    # different one.
    if "email" in id_claims and "email_verified" not in id_claims:
        same = isinstance(info.get("email"), str) and isinstance(id_claims.get("email"), str) \
            and info["email"].strip().lower() == id_claims["email"].strip().lower()
        if not same:
            out.pop("email_verified", None)
    return out
