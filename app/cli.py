import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.security import hash_password
from app.time import utc_now


def create_user(username: str, password: str) -> None:
    username = username.strip()
    if len(username) < 3:
        raise ValueError("Username must contain at least 3 characters")
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")

    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise ValueError(f"User {username!r} already exists")
        now = utc_now()
        db.add(
            User(
                username=username,
                password_hash=hash_password(password),
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gamerate")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user", help="Create a local application user")
    create.add_argument("username")
    create.add_argument("--password", help="Avoid in shell history; omit to prompt securely")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "create-user":
        password = args.password or getpass.getpass("Password: ")
        confirmation = args.password or getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match", file=sys.stderr)
            raise SystemExit(2)
        try:
            create_user(args.username, password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"Created user {args.username!r}")


if __name__ == "__main__":
    main()
