"""First-admin onboarding for a managed deployment.

A ``managed`` server needs one control-plane admin to exist before anyone can be
provisioned. This module seeds that first admin **once**, at startup, from a
bootstrap secret — then goes inert. It is deliberately separate from
:mod:`memgres.identity` (pure DB logic) because it also does file I/O and
logging.

Invariants (see the epic ``meta.memgres.admin_as_role_and_mcp``):

* seeding fires **only when the database holds zero admin users** — a fresh
  install. Once any admin exists, the env/file secret is inert (never a standing
  backdoor); the stored token authenticates its real user from then on.
* the secret is stored **hashed, as an ordinary token** of the seeded user, so
  the same value later resolves to that attributed user, not an anonymous root.
* only ``managed`` mode bootstraps; ``single``/``open`` are untouched.

Secret precedence (config rejects setting both):

* ``MEMGRES_ADMIN_TOKEN`` — the secret itself, in the env.
* ``MEMGRES_ADMIN_TOKEN_FILE`` — a path, **read-or-create** (Jenkins-style):
  present and non-empty → read it; missing/empty → generate an ``mgk_`` token,
  write it ``0600``, and log the **path only** (never the secret). The operator
  copies it out and deletes the file on their own schedule.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from . import identity
from .config import Config

log = logging.getLogger("memgres.bootstrap")


class BootstrapError(RuntimeError):
    """The bootstrap configuration is unusable (bad secret, unwritable file)."""


def _read_or_create_token_file(path: str) -> Tuple[str, Optional[str]]:
    """Return ``(secret, generated_path)``. If the file has a token, read it
    (``generated_path`` None). If missing/empty, generate a fresh ``mgk_`` token,
    write it ``0600``, and return the path so the caller can log it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
    except FileNotFoundError:
        existing = ""
    except OSError as e:                                  # unreadable → fail loud
        raise BootstrapError(f"cannot read MEMGRES_ADMIN_TOKEN_FILE {path!r}: {e}")
    if existing:
        return existing, None

    secret = identity.new_token()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret + "\n")
        os.chmod(path, 0o600)            # tighten even if the file pre-existed
    except OSError as e:
        raise BootstrapError(f"cannot write MEMGRES_ADMIN_TOKEN_FILE {path!r}: {e}")
    return secret, path


def _bootstrap_secret(cfg: Config) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the bootstrap secret from config → ``(secret, generated_path)``.
    ``(None, None)`` when the operator supplied no source."""
    if cfg.admin_token_file:
        return _read_or_create_token_file(cfg.admin_token_file)
    if cfg.admin_token:
        return cfg.admin_token, None
    return None, None


def bootstrap_admin(conn, cfg: Config) -> Optional[str]:
    """Seed the first service admin if the database has none. Returns the seeded
    user's id, or ``None`` when nothing was seeded (not managed, admins already
    exist, or no bootstrap source). Idempotent — safe to call on every startup.

    Runs in its own transaction so a partial seed never commits."""
    if cfg.key_mode != "managed":
        return None

    with conn.transaction():
        if identity.count_service_admins(conn) > 0:
            return None                  # control plane exists → env/file inert

        secret, generated_path = _bootstrap_secret(cfg)
        if not secret:
            log.warning(
                "memgres: managed mode has no service admin and neither "
                "MEMGRES_ADMIN_TOKEN nor MEMGRES_ADMIN_TOKEN_FILE is set. "
                "No one can be provisioned until you seed an admin — set a "
                "bootstrap token, or run the memgres-grant-superadmin CLI.")
            return None
        if not identity.valid_format(secret):
            # A legacy or weak env secret: don't crash (it still works as the
            # anonymous break-glass root via identity.resolve), but it can't be
            # stored as an attributable token. Warn and leave the DB adminless.
            log.warning(
                "memgres: the bootstrap token is not a strong mgk_ token, so no "
                "attributable superadmin was seeded — it still works as the "
                "anonymous env root. For an attributed admin, set a strong "
                "MEMGRES_ADMIN_TOKEN (mgk_ + 43 url-safe chars) or leave "
                "MEMGRES_ADMIN_TOKEN_FILE empty to have one generated.")
            return None

        uid = identity.create_user(
            conn, name="bootstrap-admin",
            description="seeded at startup from the bootstrap token",
            role=cfg.admin_role)
        identity.register_token(conn, uid, secret, permission="admin",
                                label="bootstrap")

    if generated_path:
        log.warning(
            "memgres: no admin token was provided — generated one and wrote it "
            "to %s (mode 0600). Copy it now; it is NOT logged and won't be shown "
            "again. Delete the file once you've stored the token.", generated_path)
    else:
        log.info("memgres: seeded the first service admin (role=%s) from the "
                 "bootstrap token", cfg.admin_role)
    return uid
