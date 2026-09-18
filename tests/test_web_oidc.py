"""OIDC sign-in end to end, against a provider that lives in this process.

The fake provider signs real RS256 id_tokens and checks PKCE, so what is
exercised is the actual verification path — not a mocked-out "token is valid".
"""

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("psycopg_pool")
jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from memgres import identity  # noqa: E402
from memgres.config import load  # noqa: E402
from memgres.server import create_app  # noqa: E402
from memgres.web import admission, oidc, oidc_config  # noqa: E402

DSN = os.environ.get("MEMGRES_TEST_DSN",
                     "postgresql://memgres:memgres@localhost:55432/memgres")
ORIGIN = "https://testserver"
ISSUER = "https://id.example.test"
OTHER = "https://other.example.test"


def _reachable() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no test Postgres")


# ─── a provider in memory ────────────────────────────────────────────────────
class FakeIdP:
    def __init__(self):
        self.keys = {}          # issuer -> (private_key, kid)
        self.codes = {}         # code -> dict
        self.userinfo = {}      # access token -> claims
        for iss in (ISSUER, OTHER):
            self.keys[iss] = (rsa.generate_private_key(public_exponent=65537, key_size=2048), "k1")
        self.alg = "RS256"
        self.tamper = {}        # claim overrides applied after the test's claims
        self.sign_with = None   # a key other than the published one

    def base(self, url):
        for iss in (ISSUER, OTHER):
            if url.startswith(iss):
                return iss
        raise AssertionError(f"unexpected url {url}")

    def __call__(self, method, url, *, data=None, headers=None):
        iss = self.base(url)
        path = url[len(iss):]
        if path == "/.well-known/openid-configuration":
            return {"issuer": iss, "authorization_endpoint": iss + "/authorize",
                    "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks",
                    "userinfo_endpoint": iss + "/userinfo"}
        if path == "/jwks":
            key, kid = self.keys[iss]
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
            jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
            return {"keys": [jwk]}
        if path == "/token":
            entry = self.codes.pop(data["code"], None)
            if entry is None:
                raise oidc.OIDCError("invalid_grant")
            expected = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if expected != entry["challenge"]:
                raise oidc.OIDCError("PKCE mismatch")
            now = int(time.time())
            claims = {"iss": iss, "aud": entry["client_id"], "iat": now, "exp": now + 300,
                      "nonce": entry["nonce"], **entry["claims"], **self.tamper}
            key, kid = self.keys[iss]
            if self.alg == "HS256":
                token = jwt.encode(claims, "shared", algorithm="HS256")
            else:
                token = jwt.encode(claims, self.sign_with or key, algorithm=self.alg, headers={"kid": kid})
            access = "at-" + data["code"]
            self.userinfo[access] = {"sub": entry["claims"]["sub"], **entry.get("userinfo", {})}
            return {"id_token": token, "access_token": access, "token_type": "Bearer"}
        if path == "/userinfo":
            return self.userinfo[headers["Authorization"].split(" ", 1)[1]]
        raise AssertionError(f"unexpected {method} {url}")


PROVIDERS = """
[providers.corp]
label = "Company SSO"
issuer = "https://id.example.test"
client_id = "memgres"
on_email_match = "link"
on_no_match = "create"
allowed_email_domains = ["example.com"]
[[providers.corp.require]]
claim = "roles"
has = "memgres"

[providers.google]
label = "Google"
issuer = "https://other.example.test"
client_id = "memgres-g"
"""


@pytest.fixture
def box(monkeypatch, tmp_path):
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for k in list(os.environ):
        if k.startswith("MEMGRES_"):
            monkeypatch.delenv(k, raising=False)
    conf = tmp_path / "providers.toml"
    conf.write_text(PROVIDERS)
    root = identity.new_token()
    for k, v in {"MEMGRES_DATABASE_URL": DSN, "MEMGRES_KEY_MODE": "managed",
                 "MEMGRES_EMBED_PROVIDER": "none", "MEMGRES_FTS_LANGUAGE": "simple",
                 "MEMGRES_REQUIRE_TITLE": "false", "MEMGRES_ADMIN_TOKEN": root,
                 "MEMGRES_ADMIN_ROLE": "superadmin", "MEMGRES_WEB_ENABLED": "true", "MEMGRES_PUBLIC_URL": "https://testserver",
                 "MEMGRES_OIDC_CONFIG": str(conf)}.items():
        monkeypatch.setenv(k, v)
    idp = FakeIdP()
    monkeypatch.setattr(oidc, "http_json", idp)
    cfg = load()
    with TestClient(create_app(cfg), base_url=ORIGIN) as c:
        yield c, root, idp, cfg


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


def sign_in(client, idp, provider="corp", *, claims, userinfo=None, link=False, code="c1"):
    """Drive one browser round trip: start → provider → callback. Returns the
    callback's redirect location."""
    if link:
        csrf = client.get("/ui/api/session").json()["csrf"]
        r = client.post("/ui/api/me/signins/link", json={"provider": provider},
                        headers={"Origin": ORIGIN, "X-Memgres-CSRF": csrf})
        assert r.status_code == 200, r.text
        url = r.json()["url"]
    else:
        r = client.get(f"/ui/auth/{provider}/start", follow_redirects=False)
        if r.status_code != 302:
            return r.headers.get("location")
        url = r.headers["location"]
    q = parse_qs(urlsplit(url).query)
    idp.codes[code] = {"claims": claims, "nonce": q["nonce"][0], "challenge": q["code_challenge"][0],
                       "client_id": q["client_id"][0], "userinfo": userinfo or {}}
    back = client.get(f"/ui/auth/{provider}/callback", params={"code": code, "state": q["state"][0]},
                      follow_redirects=False)
    assert back.status_code == 303, back.text
    return back.headers["location"]


EMP = {"sub": "emp-1", "email": "mark@example.com", "email_verified": True, "name": "Mark Levin", "roles": ["memgres"]}


def _users(cur):
    cur.execute("SELECT count(*) FROM app_user")
    return cur.fetchone()[0]


# ─── admission ───────────────────────────────────────────────────────────────
def test_an_employee_is_created_and_signed_in(box):
    client, _, idp, _ = box
    assert sign_in(client, idp, claims=EMP) == "/memory"
    me = client.get("/ui/api/session").json()
    assert me["via"] == "oidc:corp" and me["user"]["email"] == "mark@example.com"
    assert me["user"]["full_name"] == "Mark Levin" and me["role"] == "user"
    assert client.get("/ui/api/spaces").json()["spaces"] == []   # access is still granted by a person


def test_the_second_sign_in_uses_the_link_not_the_email(box):
    client, _, idp, _ = box
    sign_in(client, idp, claims=EMP)
    uid = client.get("/ui/api/session").json()["user"]["id"]
    client.cookies.clear()
    moved = {**EMP, "email": "mark.levin@example.com"}
    assert sign_in(client, idp, claims=moved, code="c2") == "/memory"
    assert client.get("/ui/api/session").json()["user"]["id"] == uid


def test_roles_come_from_userinfo_too(box):
    client, _, idp, _ = box
    no_roles = {k: v for k, v in EMP.items() if k != "roles"}
    assert sign_in(client, idp, claims=no_roles, userinfo={"roles": ["memgres"]}) == "/memory"


def test_failing_the_rule_creates_nothing(box):
    client, _, idp, _ = box
    stranger = {**EMP, "roles": ["franchisee"]}
    assert sign_in(client, idp, claims=stranger) == "/signin?auth=denied_rules"
    assert client.get("/ui/api/session").status_code == 401
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        before = _users(cur)
        cur.execute("SELECT count(*) FROM signin_request")
        assert cur.fetchone()[0] == 0
    assert before == 1    # only the bootstrap admin


def test_a_foreign_email_domain_is_refused(box):
    client, _, idp, _ = box
    assert sign_in(client, idp, claims={**EMP, "email": "mark@gmail.com"}) == "/signin?auth=denied_rules"


def test_an_unverified_email_does_not_count_for_the_domain_rule(box):
    client, _, idp, _ = box
    assert sign_in(client, idp, claims={**EMP, "email_verified": False}) == "/signin?auth=denied_rules"


def test_managed_defaults_refuse_a_stranger(box):
    """google has no rules of its own: managed mode means pending on a match, deny otherwise."""
    client, _, idp, _ = box
    assert sign_in(client, idp, "google", claims={"sub": "g-1", "email": "x@gmail.com",
                                                  "email_verified": True}) == "/signin?auth=denied_no_account"


def test_an_email_match_links_a_plain_user(box):
    client, root, idp, _ = box
    uid = client.post("/admin/users", json={"name": "mark", "email": "mark@example.com"},
                      headers=_bearer(root)).json()["id"]
    assert sign_in(client, idp, claims=EMP) == "/memory"
    assert client.get("/ui/api/session").json()["user"]["id"] == uid


def test_an_email_match_never_links_an_administrator_by_itself(box):
    client, root, idp, _ = box
    client.post("/admin/users", json={"name": "boss", "email": "mark@example.com", "role": "superadmin"},
                headers=_bearer(root))
    assert sign_in(client, idp, claims=EMP) == "/signin?auth=pending"
    assert client.get("/ui/api/session").status_code == 401


def test_an_unverified_email_links_nobody(box):
    client, root, idp, _ = box
    uid = client.post("/admin/users", json={"name": "mark", "email": "mark@example.com"},
                      headers=_bearer(root)).json()["id"]
    # google: managed defaults; an unverified email matches nobody — and the person
    # is told it is the confirmation that is missing, not an account
    assert sign_in(client, idp, "google", claims={"sub": "g-1", "email": "mark@example.com",
                                                  "email_verified": False}) == "/signin?auth=denied_email_unverified"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user_identity WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] == 0


# ─── pending → an administrator decides ──────────────────────────────────────
def _admin_client(client, root):
    client.cookies.clear()
    client.post("/ui/api/session/token", json={"token": root}, headers={"Origin": ORIGIN})
    return {"Origin": ORIGIN, "X-Memgres-CSRF": client.get("/ui/api/session").json()["csrf"]}


def test_pending_then_approved_by_linking(box):
    client, root, idp, _ = box
    uid = client.post("/admin/users", json={"name": "mark", "email": "mark@gmail.com"},
                      headers=_bearer(root)).json()["id"]
    g = {"sub": "g-1", "email": "mark@gmail.com", "email_verified": True, "name": "Mark"}
    assert sign_in(client, idp, "google", claims=g) == "/signin?auth=pending"
    # signing in again while waiting does not make a second request
    assert sign_in(client, idp, "google", claims=g, code="c2") == "/signin?auth=pending"

    h = _admin_client(client, root)
    assert client.get("/ui/api/session").json()["pending_requests"] == 1
    reqs = client.get("/ui/api/admin/signin-requests").json()["requests"]
    assert len(reqs) == 1 and reqs[0]["suggested"]["id"] == uid
    r = client.post(f"/ui/api/admin/signin-requests/{reqs[0]['id']}", json={"action": "link"}, headers=h)
    assert r.status_code == 200 and r.json()["user_id"] == uid

    client.cookies.clear()
    assert sign_in(client, idp, "google", claims=g, code="c3") == "/memory"
    assert client.get("/ui/api/session").json()["user"]["id"] == uid


def test_rejected_stays_rejected_for_a_while(box):
    client, root, idp, _ = box
    client.post("/admin/users", json={"name": "mark", "email": "mark@gmail.com"}, headers=_bearer(root))
    g = {"sub": "g-1", "email": "mark@gmail.com", "email_verified": True}
    sign_in(client, idp, "google", claims=g)
    h = _admin_client(client, root)
    rid = client.get("/ui/api/admin/signin-requests").json()["requests"][0]["id"]
    assert client.post(f"/ui/api/admin/signin-requests/{rid}", json={"action": "reject"}, headers=h).status_code == 200
    client.cookies.clear()
    assert sign_in(client, idp, "google", claims=g, code="c2") == "/signin?auth=denied_rejected"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM signin_request WHERE status = 'pending'")
        assert cur.fetchone()[0] == 0


def test_a_user_manager_cannot_link_a_sign_in_to_a_superadmin(box):
    client, root, idp, cfg = box
    boss = client.post("/admin/users", json={"name": "boss", "email": "mark@example.com", "role": "superadmin"},
                       headers=_bearer(root)).json()["id"]
    sign_in(client, idp, claims=EMP)                                # pending: admin match
    mgr = client.post("/admin/users", json={"name": "olga", "role": "user_manager"}, headers=_bearer(root)).json()["id"]
    mgr_tok = client.post("/admin/tokens", json={"user_id": mgr, "permission": "admin"},
                          headers=_bearer(root)).json()["token"]
    h = _admin_client(client, mgr_tok)
    rid = client.get("/ui/api/admin/signin-requests").json()["requests"][0]["id"]
    r = client.post(f"/ui/api/admin/signin-requests/{rid}", json={"action": "link", "user_id": boss}, headers=h)
    assert r.status_code == 403


def test_plain_users_cannot_see_the_queue(box):
    client, _, idp, _ = box
    sign_in(client, idp, claims=EMP)
    assert client.get("/ui/api/admin/signin-requests").status_code == 403


# ─── the flow itself ─────────────────────────────────────────────────────────
def _start(client, provider="corp"):
    r = client.get(f"/ui/auth/{provider}/start", follow_redirects=False)
    return parse_qs(urlsplit(r.headers["location"]).query)


def test_state_is_single_use(box):
    client, _, idp, _ = box
    q = _start(client)
    idp.codes["c1"] = {"claims": EMP, "nonce": q["nonce"][0], "challenge": q["code_challenge"][0], "client_id": "memgres"}
    first = client.get("/ui/auth/corp/callback", params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)
    assert first.headers["location"] == "/memory"
    again = client.get("/ui/auth/corp/callback", params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)
    assert again.headers["location"] == "/signin?auth=expired"


def test_a_callback_in_another_browser_completes_nothing(box):
    """Login CSRF: an attacker's own callback URL planted in a victim's browser
    must not sign the victim in as the attacker."""
    client, _, idp, _ = box
    q = _start(client)
    idp.codes["c1"] = {"claims": EMP, "nonce": q["nonce"][0], "challenge": q["code_challenge"][0], "client_id": "memgres"}
    client.cookies.clear()
    r = client.get("/ui/auth/corp/callback", params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)
    assert r.headers["location"] == "/signin?auth=expired"
    assert client.get("/ui/api/session").status_code == 401


@pytest.mark.parametrize("tamper", [
    {"nonce": "someone-elses"},
    {"aud": "another-client"},
    {"iss": "https://evil.example.test"},
    {"exp": 1000},
])
def test_a_bad_id_token_is_refused(box, tamper):
    client, _, idp, _ = box
    idp.tamper = tamper
    assert sign_in(client, idp, claims=EMP) == "/signin?auth=failed"
    assert client.get("/ui/api/session").status_code == 401


def test_a_shared_secret_signature_is_refused(box):
    client, _, idp, _ = box
    idp.alg = "HS256"
    assert sign_in(client, idp, claims=EMP) == "/signin?auth=failed"


def test_a_token_signed_by_another_key_is_refused(box):
    """Same kid, different key: exactly what a forged token looks like."""
    client, _, idp, _ = box
    idp.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert sign_in(client, idp, claims=EMP) == "/signin?auth=failed"
    assert client.get("/ui/api/session").status_code == 401


def test_discovery_must_name_the_configured_issuer(box):
    _, _, idp, _ = box

    def lying(method, url, **kw):
        out = idp(method, url, **kw)
        if url.endswith("openid-configuration"):
            out = {**out, "issuer": "https://evil.example.test"}
        return out
    provider = oidc_config.parse({"providers": {"x": {"issuer": ISSUER, "client_id": "m"}}},
                                 key_mode="managed")["x"]
    with pytest.raises(oidc.OIDCError, match="issuer"):
        oidc.Client(provider, fetch=lying).metadata()


def test_a_provider_that_is_down_is_an_error_not_a_crash(box):

    def down(method, url, **kw):
        raise oidc.OIDCError("connection refused")
    provider_client = oidc.Client(oidc_config.parse(
        {"providers": {"x": {"issuer": ISSUER, "client_id": "m"}}}, key_mode="managed")["x"], fetch=down)
    with pytest.raises(oidc.OIDCError):
        provider_client.authorize_url(redirect_uri="https://t/cb", state="s", nonce="n", challenge="c")


# ─── a person's sign-in methods ──────────────────────────────────────────────
def test_link_a_second_method_then_unlink_it(box):
    client, _, idp, _ = box
    sign_in(client, idp, claims=EMP)
    me = client.get("/ui/api/session").json()
    h = {"Origin": ORIGIN, "X-Memgres-CSRF": me["csrf"]}
    assert sign_in(client, idp, "google", claims={"sub": "g-9", "email": "m@gmail.com", "email_verified": True},
                   link=True, code="c2") == "/account/signins?auth=linked"
    methods = client.get("/ui/api/me/signins").json()["signins"]
    assert {m["provider"] for m in methods} == {"corp", "google"}
    google = next(m for m in methods if m["provider"] == "google")
    assert client.delete(f"/ui/api/me/signins/{google['id']}", headers=h).status_code == 200
    corp = client.get("/ui/api/me/signins").json()["signins"][0]
    assert client.delete(f"/ui/api/me/signins/{corp['id']}", headers=h).status_code == 409


def test_a_method_already_linked_elsewhere_is_not_taken_over(box):
    client, _, idp, _ = box
    olga = {**EMP, "sub": "emp-2", "email": "olga@example.com"}
    g = {"sub": "g-1", "email": "o@gmail.com", "email_verified": True}
    sign_in(client, idp, "corp", claims=olga)
    assert sign_in(client, idp, "google", claims=g, link=True, code="c2") == "/account/signins?auth=linked"
    client.cookies.clear()
    sign_in(client, idp, "corp", claims=EMP, code="c3")                  # mark
    assert sign_in(client, idp, "google", claims=g, link=True, code="c4") == "/account/signins?auth=denied_taken"


def test_a_second_account_at_the_same_provider_is_not_linked_by_the_person(box):
    client, _, idp, _ = box
    sign_in(client, idp, "corp", claims=EMP)
    assert sign_in(client, idp, "corp", claims={**EMP, "sub": "emp-99"}, link=True,
                   code="c2") == "/account/signins?auth=denied_same_provider"


def test_linking_needs_a_session_and_the_csrf_token(box):
    client, _, idp, _ = box
    assert client.post("/ui/api/me/signins/link", json={"provider": "google"},
                       headers={"Origin": ORIGIN}).status_code == 401
    sign_in(client, idp, claims=EMP)
    # a signed-in browser, but a request without the token: another same-site page
    assert client.post("/ui/api/me/signins/link", json={"provider": "google"},
                       headers={"Origin": ORIGIN}).status_code == 403


def test_a_link_started_by_a_session_that_has_since_ended_completes_nothing(box):
    client, _, idp, _ = box
    sign_in(client, idp, claims=EMP)
    csrf = client.get("/ui/api/session").json()["csrf"]
    url = client.post("/ui/api/me/signins/link", json={"provider": "google"},
                      headers={"Origin": ORIGIN, "X-Memgres-CSRF": csrf}).json()["url"]
    q = parse_qs(urlsplit(url).query)
    client.delete("/ui/api/session", headers={"Origin": ORIGIN, "X-Memgres-CSRF": csrf})
    idp.codes["late"] = {"claims": {"sub": "g-attacker", "email": "a@gmail.com", "email_verified": True},
                         "nonce": q["nonce"][0], "challenge": q["code_challenge"][0], "client_id": "memgres-g"}
    r = client.get("/ui/auth/google/callback", params={"code": "late", "state": q["state"][0]}, follow_redirects=False)
    assert r.headers["location"] == "/account/signins?auth=expired"
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app_user_identity WHERE subject = 'g-attacker'")
        assert cur.fetchone()[0] == 0


def test_starting_sign_ins_is_limited_per_client(box):
    client, _, _, _ = box
    from memgres.web import auth_routes
    codes = [client.get("/ui/auth/corp/start", follow_redirects=False).status_code
             for _ in range(auth_routes.STARTS_PER_CLIENT + 1)]
    assert codes[:-1] == [302] * auth_routes.STARTS_PER_CLIENT and codes[-1] == 429


def test_a_request_needs_a_verified_email(box):
    """Otherwise anyone who can make accounts at an open provider fills the queue."""
    client, root, idp, cfg = box
    provider = oidc_config.parse({"providers": {"g": {"issuer": OTHER, "client_id": "m", "on_no_match": "pending"}}},
                                 key_mode="managed")["g"]
    with psycopg.connect(DSN) as conn:
        assert admission.admit(conn, cfg, provider, {"sub": "x1"}).reason == "email_unverified"
        assert admission.admit(conn, cfg, provider, {"sub": "x2", "email": "a@b.c",
                                                     "email_verified": False}).reason == "email_unverified"
        assert admission.admit(conn, cfg, provider, {"sub": "x3", "email": "a@b.c",
                                                     "email_verified": True}).kind == "pending"


def test_one_provider_cannot_fill_the_whole_queue(box, monkeypatch):
    client, root, idp, cfg = box
    monkeypatch.setattr(admission, "MAX_PENDING_PER_PROVIDER", 2)
    parse = lambda pid, iss: oidc_config.parse(  # noqa: E731
        {"providers": {pid: {"issuer": iss, "client_id": "m", "on_no_match": "pending"}}}, key_mode="managed")[pid]
    noisy, quiet = parse("noisy", OTHER), parse("quiet", ISSUER)
    person = lambda i: {"sub": f"s{i}", "email": f"p{i}@x.test", "email_verified": True}  # noqa: E731
    with psycopg.connect(DSN) as conn:
        kinds = [admission.admit(conn, cfg, noisy, person(i)).kind for i in range(3)]
        assert kinds == ["pending", "pending", "denied"]
        assert admission.admit(conn, cfg, quiet, person(9)).kind == "pending"


def test_unlinking_ends_only_that_methods_sessions(box):
    client, _, idp, _ = box
    sign_in(client, idp, claims=EMP)                                   # session A via corp
    corp_cookie = client.cookies.get("__Host-memgres_session")
    assert sign_in(client, idp, "google", claims={"sub": "g-9", "email": "m@gmail.com", "email_verified": True},
                   link=True, code="c2") == "/account/signins?auth=linked"
    client.cookies.clear()
    sign_in(client, idp, "google", claims={"sub": "g-9", "email": "m@gmail.com", "email_verified": True}, code="c3")
    csrf = client.get("/ui/api/session").json()["csrf"]
    methods = client.get("/ui/api/me/signins").json()["signins"]
    google = next(m for m in methods if m["provider"] == "google")
    assert client.delete(f"/ui/api/me/signins/{google['id']}",
                         headers={"Origin": ORIGIN, "X-Memgres-CSRF": csrf}).status_code == 200
    assert client.get("/ui/api/session").status_code == 401            # the google session ended
    client.cookies.set("__Host-memgres_session", corp_cookie)
    assert client.get("/ui/api/session").status_code == 200            # the corp one did not


def test_cut_off_spares_the_bootstrap_token_in_file_mode(box):
    """With MEMGRES_ADMIN_TOKEN_FILE, cfg.admin_token is empty; the seeded token
    is still recognised by its label."""
    import dataclasses
    client, root, idp, cfg = box
    file_mode = dataclasses.replace(cfg, admin_token="")
    provider = oidc_config.parse({"providers": {"corp": {
        "issuer": ISSUER, "client_id": "memgres", "sync_on_login": True,
        "require": [{"claim": "roles", "has": "memgres"}]}}}, key_mode="managed")["corp"]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM token WHERE label = 'bootstrap'")
        boot_uid = str(cur.fetchone()[0])
        cur.execute("INSERT INTO app_user_identity (issuer, subject, user_id, provider) VALUES (%s, 'boot', %s, 'corp')",
                    (ISSUER, boot_uid))
        conn.commit()
        assert admission.admit(conn, file_mode, provider, {"sub": "boot", "roles": []}).kind == "denied"
        conn.commit()
    assert client.get("/whoami", headers=_bearer(root)).status_code == 200


def test_losing_the_role_cuts_a_synced_account_off(box, tmp_path, monkeypatch):
    client, root, idp, cfg = box
    provider = oidc_config.parse({"providers": {"corp": {
        "issuer": ISSUER, "client_id": "memgres", "on_email_match": "link", "on_no_match": "create",
        "sync_on_login": True, "require": [{"claim": "roles", "has": "memgres"}]}}}, key_mode="managed")["corp"]
    with psycopg.connect(DSN) as conn:
        out = admission.admit(conn, cfg, provider, EMP)
        uid = out.user_id
        _, tok = None, identity.issue_token(conn, uid)[0]
        conn.commit()
        out = admission.admit(conn, cfg, provider, {**EMP, "roles": []})
        conn.commit()
    assert out.kind == "denied"
    assert client.get("/whoami", headers=_bearer(tok)).status_code in (401, 403)
    assert client.get("/whoami", headers=_bearer(root)).status_code == 200


def test_admin_redirect_stops_once_an_admin_links_a_provider(box):
    client, root, idp, _ = box
    assert client.get("/signin", follow_redirects=False).status_code == 307
    uid = client.post("/admin/users", json={"name": "boss", "email": "boss@example.com", "role": "user_manager"},
                      headers=_bearer(root)).json()["id"]
    tok = client.post("/admin/tokens", json={"user_id": uid, "permission": "admin"},
                      headers=_bearer(root)).json()["token"]
    client.post("/ui/api/session/token", json={"token": tok}, headers={"Origin": ORIGIN})
    assert sign_in(client, idp, "corp", claims={**EMP, "sub": "boss-1", "email": "boss@example.com"},
                   link=True) == "/account/signins?auth=linked"
    assert client.get("/signin", follow_redirects=False).status_code == 200
    names = [p["id"] for p in client.get("/ui/api/signin-options").json()["providers"]]
    assert names == ["corp", "google"]


# ─── the config file ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad, match", [
    ({"providers": {}}, "no \\[providers"),
    ({"providers": {"x": {"issuer": ISSUER}}}, "client_id"),
    ({"providers": {"x": {"issuer": "http://id.test", "client_id": "m"}}}, "https"),
    ({"providers": {"x": {"issuer": ISSUER, "client_id": "m", "on_no_mach": "create"}}}, "unknown keys"),
    ({"providers": {"x": {"issuer": ISSUER, "client_id": "m", "on_no_match": "allow"}}}, "on_no_match"),
    ({"providers": {"x": {"issuer": ISSUER, "client_id": "m", "require": [{"claim": "roles"}]}}}, "require"),
    ({"providers": {"X Y": {"issuer": ISSUER, "client_id": "m"}}}, "provider id"),
    ({"providers": {"x": {"issuer": ISSUER, "client_id": "m", "token_auth": "client_secret_basic"}}}, "client_secret_file"),
])
def test_a_broken_provider_config_is_refused(bad, match):
    with pytest.raises(oidc_config.OIDCConfigError, match=match):
        oidc_config.parse(bad, key_mode="managed")


def test_modes_supply_defaults_only():
    base = {"providers": {"x": {"issuer": ISSUER, "client_id": "m"}}}
    managed = oidc_config.parse(base, key_mode="managed")["x"]
    open_ = oidc_config.parse(base, key_mode="open")["x"]
    assert (managed.on_email_match, managed.on_no_match) == ("pending", "deny")
    assert (open_.on_email_match, open_.on_no_match) == ("link", "create")


# ─── invitations meet a sign-in ──────────────────────────────────────────────
def _space_with_invite(client, root, email, permission="write"):
    owner = client.post("/admin/users", json={"name": "owner"}, headers=_bearer(root)).json()["id"]
    space = client.post("/admin/namespaces", json={"owner_user_id": owner, "name": "sales"},
                        headers=_bearer(root)).json()["id"]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO space_invite (namespace_id, email, permission, invited_by, expires_at) "
                    "VALUES (%s, %s, %s, %s, now() + interval '1 day')", (space, email, permission, owner))
    return space


def test_an_invitation_opens_the_space_on_the_first_sign_in(box):
    client, root, idp, _ = box
    space = _space_with_invite(client, root, "Mark@Example.com")
    assert sign_in(client, idp, claims=EMP) == "/memory"
    got = client.get("/ui/api/spaces").json()["spaces"]
    assert [(s["id"], s["permission"]) for s in got] == [(space, "write")]


def test_an_invitation_does_not_skip_the_providers_rules_or_the_queue(box):
    client, root, idp, _ = box
    _space_with_invite(client, root, "mark@gmail.com")
    g = {"sub": "g-1", "email": "mark@gmail.com", "email_verified": True, "name": "Mark"}
    # google, managed defaults: nobody matches → refused, invitation or not
    assert sign_in(client, idp, "google", claims=g) == "/signin?auth=denied_no_account"
    # an email match on google waits for an administrator — who sees the invitation
    client.post("/admin/users", json={"name": "mark", "email": "mark@gmail.com"}, headers=_bearer(root))
    assert sign_in(client, idp, "google", claims=g, code="c2") == "/signin?auth=pending"
    _admin_client(client, root)
    req = client.get("/ui/api/admin/signin-requests").json()["requests"][0]
    assert req["invites"] == [{"space": "sales", "by": "owner", "permission": "write"}]



def test_userinfo_cannot_vouch_for_an_address_it_does_not_name():
    id_claims = {"sub": "s", "email": "boss@example.com"}
    info = {"sub": "s", "email": "me@example.com", "email_verified": True}
    assert "email_verified" not in oidc.merged_claims(id_claims, info)
    assert oidc.merged_claims(id_claims, {**info, "email": "BOSS@example.com"})["email_verified"] is True
    assert oidc.merged_claims({**id_claims, "email_verified": False}, info)["email_verified"] is False


def test_a_verified_stranger_is_still_told_there_is_no_account(box):
    client, _, idp, _ = box
    assert sign_in(client, idp, "google", claims={"sub": "g-9", "email": "x@gmail.com",
                                                  "email_verified": True}) == "/signin?auth=denied_no_account"


def test_linking_asks_the_provider_to_let_the_person_choose(box):
    """A browser often holds another session at the provider — a service admin's
    — and without this the provider hands that one back without asking."""
    client, root, idp, _ = box
    _admin_client(client, root)
    csrf = client.get("/ui/api/session").json()["csrf"]
    url = client.post("/ui/api/me/signins/link", json={"provider": "corp"},
                      headers={"Origin": ORIGIN, "X-Memgres-CSRF": csrf}).json()["url"]
    assert parse_qs(urlsplit(url).query)["prompt"] == ["select_account"]
    plain = client.get("/ui/auth/corp/start", follow_redirects=False).headers["location"]
    assert "prompt" not in parse_qs(urlsplit(plain).query)


def test_the_access_log_does_not_keep_the_code(box):
    import logging
    from memgres.web.routes import _ScrubSignInQuery
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:0", "GET", "/ui/auth/corp/callback?code=SECRET&state=S", "1.1", 303), None)
    _ScrubSignInQuery().filter(rec)
    assert "SECRET" not in rec.getMessage() and "/ui/auth/corp/callback" in rec.getMessage()
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                              ("1.2.3.4:0", "GET", "/ui/api/spaces?x=1", "1.1", 200), None)
    _ScrubSignInQuery().filter(other)
    assert "?x=1" in other.getMessage()


def test_pages_answer_head(box):
    client, _, _, _ = box
    for path in ("/signin", "/signin/admin", "/memory", "/account"):
        assert client.head(path, follow_redirects=False).status_code in (200, 307), path
