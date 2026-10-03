"""The one admin account: password, TOTP secret and backup codes."""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from dataclasses import dataclass

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

from .config import Settings
from .db import Store

ISSUER = "dashboard-x"
TOTP_STEP = 30
BACKUP_CODE_COUNT = 10
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()  # argon2id with the library's current defaults
# Verified against when the username is wrong, so a wrong name and a wrong
# password take the same time.
_DUMMY_HASH = _hasher.hash("dashboard-x dummy password")


class AdminError(Exception):
    pass


@dataclass(frozen=True)
class Enrolment:
    otpauth_uri: str
    backup_codes: list[str]


def _fernet(settings: Settings) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(settings.subkey("totp-secret")))


def _verify_hash(hash_: str, secret: str) -> bool:
    try:
        return _hasher.verify(hash_, secret)
    except (VerificationError, InvalidHashError):
        return False


def _new_backup_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
    codes = []
    for _ in range(BACKUP_CODE_COUNT):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def _normalise_backup_code(code: str) -> str:
    raw = "".join(ch for ch in code.lower() if ch.isalnum())
    return f"{raw[:5]}-{raw[5:]}"


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AdminError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")


class Admin:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    # -- reading -------------------------------------------------------------

    def exists(self) -> bool:
        with self.store.connect() as conn:
            return conn.execute("SELECT 1 FROM admin WHERE id = 1").fetchone() is not None

    def username(self) -> str | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT username FROM admin WHERE id = 1").fetchone()
        return row["username"] if row else None

    def credentials_changed_at(self) -> float:
        with self.store.connect() as conn:
            row = conn.execute("SELECT credentials_changed_at FROM admin WHERE id = 1").fetchone()
        return row["credentials_changed_at"] if row else 0.0

    def unused_backup_codes(self) -> int:
        with self.store.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM backup_codes WHERE used_at IS NULL").fetchone()
        return row["n"]

    # -- changing (CLI only) -------------------------------------------------

    def create(self, username: str, password: str) -> Enrolment:
        username = username.strip()
        if not username:
            raise AdminError("The username must not be empty.")
        check_new_password(password)
        if self.exists():
            raise AdminError("An admin already exists. Use reset-password or reset-totp.")
        secret = pyotp.random_base32()
        codes = _new_backup_codes()
        now = time.time()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO admin (id, username, password_hash, totp_secret_enc, credentials_changed_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (username, _hasher.hash(password), self._encrypt(secret), now),
            )
            self._store_backup_codes(conn, codes)
        return Enrolment(self._otpauth_uri(username, secret), codes)

    def reset_password(self, password: str) -> None:
        check_new_password(password)
        self._require()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE admin SET password_hash = ?, credentials_changed_at = ? WHERE id = 1",
                (_hasher.hash(password), time.time()),
            )
            conn.execute("DELETE FROM sessions")

    def reset_totp(self) -> Enrolment:
        username = self._require()
        secret = pyotp.random_base32()
        codes = _new_backup_codes()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE admin SET totp_secret_enc = ?, last_totp_step = 0, credentials_changed_at = ? WHERE id = 1",
                (self._encrypt(secret), time.time()),
            )
            self._store_backup_codes(conn, codes)
            conn.execute("DELETE FROM sessions")
        return Enrolment(self._otpauth_uri(username, secret), codes)

    # -- login checks --------------------------------------------------------

    def check_password(self, username: str, password: str) -> bool:
        with self.store.connect() as conn:
            row = conn.execute("SELECT username, password_hash FROM admin WHERE id = 1").fetchone()
        if row is None or not hmac.compare_digest(row["username"].encode(), username.strip().encode()):
            _verify_hash(_DUMMY_HASH, password)
            return False
        return _verify_hash(row["password_hash"], password)

    def check_second_factor(self, code: str, now: float | None = None) -> str | None:
        """Return "totp" or "backup" when the code is good, else None.

        A TOTP code is accepted one step either side of now, and never for a
        step at or before the last one used (no replay). A backup code works
        once.
        """
        now = time.time() if now is None else now
        code = code.strip().replace(" ", "")
        if code.isdigit() and len(code) == 6:
            return "totp" if self._check_totp(code, now) else None
        if len("".join(ch for ch in code if ch.isalnum())) == 10:
            return "backup" if self._use_backup_code(_normalise_backup_code(code), now) else None
        return None

    def _check_totp(self, code: str, now: float) -> bool:
        with self.store.connect() as conn:
            row = conn.execute("SELECT totp_secret_enc, last_totp_step FROM admin WHERE id = 1").fetchone()
            if row is None:
                return False
            totp = pyotp.TOTP(self._decrypt(row["totp_secret_enc"]))
            current = int(now // TOTP_STEP)
            for step in (current - 1, current, current + 1):
                if step <= row["last_totp_step"]:
                    continue
                if hmac.compare_digest(totp.generate_otp(step), code):
                    updated = conn.execute(
                        "UPDATE admin SET last_totp_step = ? WHERE id = 1 AND last_totp_step < ?",
                        (step, step),
                    ).rowcount
                    return updated == 1
        return False

    def _use_backup_code(self, code: str, now: float) -> bool:
        with self.store.connect() as conn:
            rows = conn.execute("SELECT id, code_hash FROM backup_codes WHERE used_at IS NULL").fetchall()
            for row in rows:
                if _verify_hash(row["code_hash"], code):
                    updated = conn.execute(
                        "UPDATE backup_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                        (now, row["id"]),
                    ).rowcount
                    return updated == 1
        return False

    # -- helpers -------------------------------------------------------------

    def _require(self) -> str:
        name = self.username()
        if name is None:
            raise AdminError("No admin exists yet. Run create-admin first.")
        return name

    def _encrypt(self, secret: str) -> str:
        return _fernet(self.settings).encrypt(secret.encode()).decode()

    def _decrypt(self, token: str) -> str:
        return _fernet(self.settings).decrypt(token.encode()).decode()

    def _otpauth_uri(self, username: str, secret: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)

    @staticmethod
    def _store_backup_codes(conn, codes: list[str]) -> None:
        conn.execute("DELETE FROM backup_codes")
        conn.executemany(
            "INSERT INTO backup_codes (code_hash) VALUES (?)",
            [(_hasher.hash(code),) for code in codes],
        )
