"""Admin commands, run on the server inside the hub container.

    python -m hub create-admin
    python -m hub reset-password
    python -m hub reset-totp
    python -m hub revoke-sessions
    python -m hub backup

There is no sign-up page: these are the only way to set or change the login.
"""

from __future__ import annotations

import argparse
import getpass
import io
import sys

import segno

from .admin import Admin, AdminError, Enrolment
from .backup import backup_now
from .config import ConfigError, load_settings
from .db import Store
from .sessions import Sessions


def _ask_password() -> str:
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Same password again: ")
    if first != second:
        raise AdminError("The two passwords are different.")
    return first


def _show_enrolment(enrolment: Enrolment) -> None:
    print()
    print("Scan this with the authenticator app (Google Authenticator, Aegis, ...):")
    buffer = io.StringIO()
    segno.make(enrolment.otpauth_uri, error="m").terminal(out=buffer, compact=True)
    print(buffer.getvalue())
    print("Or add it by hand with this link:")
    print(f"  {enrolment.otpauth_uri}")
    print()
    print("Backup codes -- each works once, in place of the 6-digit code.")
    print("Write them down somewhere safe, away from this server:")
    for code in enrolment.backup_codes:
        print(f"  {code}")
    print()
    print("They will not be shown again. Clear this terminal's scrollback now.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub", description="dashboard-x admin commands")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="create the one admin login")
    create.add_argument("--username", help="asked for when left out")
    sub.add_parser("reset-password", help="set a new password and end every session")
    sub.add_parser("reset-totp", help="new authenticator secret and backup codes; ends every session")
    sub.add_parser("revoke-sessions", help="log out everywhere")
    sub.add_parser("backup", help="copy hub.db into the backups folder now")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        store = Store(settings.db_path)
        store.init()
        admin = Admin(store, settings)

        if args.command == "create-admin":
            username = args.username or input("Username: ")
            enrolment = admin.create(username, _ask_password())
            print(f"Admin {username.strip()!r} created.")
            _show_enrolment(enrolment)
        elif args.command == "reset-password":
            admin.reset_password(_ask_password())
            print("Password changed. Every session has been ended.")
        elif args.command == "reset-totp":
            enrolment = admin.reset_totp()
            print("New authenticator secret made. The old app entry and old backup codes no longer work.")
            _show_enrolment(enrolment)
        elif args.command == "revoke-sessions":
            count = Sessions(store).delete_all()
            print(f"Ended {count} session(s).")
        elif args.command == "backup":
            print(f"Saved {backup_now(settings.db_path, settings.backups_dir)}")
    except (AdminError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
