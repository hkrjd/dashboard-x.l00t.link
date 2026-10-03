import time

import pyotp
import pytest

from hub.admin import Admin, AdminError
from hub.sessions import Sessions

from .conftest import PASSWORD, USERNAME


def enrol(admin: Admin) -> tuple[pyotp.TOTP, list[str]]:
    enrolment = admin.create(USERNAME, PASSWORD)
    return pyotp.TOTP(pyotp.parse_uri(enrolment.otpauth_uri).secret), enrolment.backup_codes


def test_create_stores_no_plain_secrets(admin, store):
    totp, codes = enrol(admin)
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM admin").fetchone())
        hashes = [r["code_hash"] for r in conn.execute("SELECT code_hash FROM backup_codes")]
    assert PASSWORD not in row["password_hash"] and row["password_hash"].startswith("$argon2id$")
    assert totp.secret not in row["totp_secret_enc"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(code not in h for code in codes for h in hashes)


def test_only_one_admin_and_password_length(admin):
    with pytest.raises(AdminError):
        admin.create(USERNAME, "short")
    enrol(admin)
    with pytest.raises(AdminError, match="already exists"):
        admin.create("someone", PASSWORD)


def test_check_password(admin):
    enrol(admin)
    assert admin.check_password(USERNAME, PASSWORD)
    assert admin.check_password(f" {USERNAME} ", PASSWORD)
    assert not admin.check_password(USERNAME, PASSWORD + "x")
    assert not admin.check_password("other", PASSWORD)


def test_totp_window_and_no_replay(admin):
    totp, _ = enrol(admin)
    now = time.time()
    step = int(now // 30)
    assert admin.check_second_factor(totp.generate_otp(step + 5), now) is None  # too far ahead
    assert admin.check_second_factor(totp.generate_otp(step - 1), now) == "totp"  # 30 s late is fine
    assert admin.check_second_factor(totp.generate_otp(step - 1), now) is None  # replay
    assert admin.check_second_factor(totp.generate_otp(step), now) == "totp"
    assert admin.check_second_factor(totp.generate_otp(step), now) is None  # replay
    assert admin.check_second_factor(totp.generate_otp(step - 1), now) is None  # older than the last one used
    assert admin.check_second_factor("12345", now) is None


def test_backup_code_works_once(admin):
    _, codes = enrol(admin)
    assert admin.check_second_factor(codes[0].upper().replace("-", " ")) == "backup"
    assert admin.check_second_factor(codes[0]) is None
    assert admin.unused_backup_codes() == 9


def test_reset_totp_replaces_secret_codes_and_sessions(admin, store):
    totp, codes = enrol(admin)
    Sessions(store).create("full", "1.2.3.4", "ua")
    enrolment = admin.reset_totp()
    assert admin.check_second_factor(codes[1]) is None
    assert pyotp.parse_uri(enrolment.otpauth_uri).secret != totp.secret
    assert Sessions(store).list_full() == []


def test_reset_password_ends_sessions(admin, store):
    enrol(admin)
    Sessions(store).create("full", "1.2.3.4", "ua")
    admin.reset_password("a brand new password")
    assert admin.check_password(USERNAME, "a brand new password")
    assert not admin.check_password(USERNAME, PASSWORD)
    assert Sessions(store).list_full() == []
