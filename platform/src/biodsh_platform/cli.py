import argparse
import getpass

import uvicorn
from sqlalchemy import select

from biodsh_platform.audit import audit
from biodsh_platform.config import get_settings
from biodsh_platform.db import SessionLocal
from biodsh_platform.models import User
from biodsh_platform.schemas import UserCreate
from biodsh_platform.security import hash_password, normalize_username


def init_admin(username: str) -> None:
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("passwords do not match")
    try:
        credentials = UserCreate(username=username, password=password, is_admin=True)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    normalized = normalize_username(credentials.username)
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)) is not None:
            raise SystemExit("an administrator already exists")
        if db.scalar(select(User.id).where(User.username == normalized)) is not None:
            raise SystemExit("username already exists")
        user = User(username=normalized, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        db.flush()
        audit(
            db,
            "admin.bootstrap.cli",
            actor_user_id=user.id,
            resource_type="user",
            resource_id=user.id,
        )
        db.commit()
        print(f"Administrator '{normalized}' created.")


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "biodsh_platform.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="biodsh-platform")
    subcommands = parser.add_subparsers(dest="command")
    admin_parser = subcommands.add_parser("init-admin", help="create the first administrator")
    admin_parser.add_argument("--username", required=True)
    args = parser.parse_args()
    if args.command == "init-admin":
        init_admin(args.username)
    else:
        serve()


if __name__ == "__main__":
    main()
