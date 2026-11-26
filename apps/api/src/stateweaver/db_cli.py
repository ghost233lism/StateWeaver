from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from stateweaver.persistence.database import SchemaStateError, verify_migration_history


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stateweaver-db",
        description="Validate the StateWeaver release schema.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check",
        help="validate all migrations and model metadata in an isolated temporary database",
    )
    parser.parse_args(argv)

    try:
        state = verify_migration_history()
    except (CommandError, OSError, SQLAlchemyError, SchemaStateError) as exc:
        print(f"stateweaver-db: {exc}", file=sys.stderr)
        return 2

    print(f"StateWeaver migration history is valid at {','.join(state.current_heads)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
