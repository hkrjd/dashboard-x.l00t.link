import os
import sqlite3

import pytest

from hub import cli
from hub.backup import backup_now
from hub.bots import load_bots


@pytest.fixture
def env(settings, monkeypatch):
    monkeypatch.setenv("HUB_SECRET_KEY", settings.secret_key.decode())
    monkeypatch.setenv("HUB_PUBLIC_ORIGIN", settings.public_origin)
    monkeypatch.setenv("HUB_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("HUB_BOTS_DIR", str(settings.bots_dir))
    return settings


def test_create_admin_prints_enrolment_once(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "a long enough password")
    assert cli.main(["create-admin", "--username", "owner"]) == 0
    out = capsys.readouterr().out
    assert "otpauth://totp/" in out and out.count("-") >= 10 and "a long enough password" not in out
    assert cli.main(["create-admin", "--username", "owner"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_mismatched_passwords_are_refused(env, monkeypatch, capsys):
    answers = iter(["first password 123", "second password 123"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    assert cli.main(["create-admin", "--username", "owner"]) == 1
    assert "different" in capsys.readouterr().err


def test_missing_secret_is_a_clear_error(env, monkeypatch, capsys):
    monkeypatch.delenv("HUB_SECRET_KEY")
    assert cli.main(["revoke-sessions"]) == 1
    assert "HUB_SECRET_KEY" in capsys.readouterr().err


def test_backup_keeps_seven(store, settings, tmp_path):
    settings.backups_dir.mkdir(parents=True)
    for day in range(1, 10):
        (settings.backups_dir / f"hub-202601{day:02d}.db").write_bytes(b"old")
    path = backup_now(settings.db_path, settings.backups_dir)
    kept = sorted(p.name for p in settings.backups_dir.glob("hub-*.db"))
    assert len(kept) == 7 and path.name in kept
    tables = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert "admin" in tables and "audit" in tables


def test_load_bots_skips_bad_folders(settings):
    (settings.bots_dir / "Bad Name").mkdir()
    (settings.bots_dir / "Bad Name" / "config.toml").write_text('api_url = "http://x"', encoding="utf-8")
    (settings.bots_dir / "notoken").mkdir()
    (settings.bots_dir / "notoken" / "config.toml").write_text('api_url = "http://notoken:8081"', encoding="utf-8")
    (settings.bots_dir / "broken").mkdir()
    (settings.bots_dir / "broken" / "config.toml").write_text("api_url = ", encoding="utf-8")
    (settings.bots_dir / "ftp").mkdir()
    (settings.bots_dir / "ftp" / "config.toml").write_text('api_url = "ftp://x"', encoding="utf-8")
    bots = load_bots(settings)
    assert sorted(bots) == ["dealops", "notoken"]
    assert bots["dealops"].configured and not bots["notoken"].configured
    assert bots["notoken"].display_name == "notoken"
    assert os.sep not in bots["dealops"].api_url
