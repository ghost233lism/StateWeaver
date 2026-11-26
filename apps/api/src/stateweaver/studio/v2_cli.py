"""Start the StateWeaver Studio from a fixed provider registry."""

import argparse
from pathlib import Path

from sqlalchemy.engine import URL

from .api import serve
from .settings import StudioSettings


def main() -> None:
    settings = StudioSettings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument(
        "--provider-ledger", type=Path, help="Use the same ledger for shared provider capacity"
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    root = args.data_root.resolve()
    config = StudioSettings(
        **{
            **settings.model_dump(),
            "provider_ledger_path": (args.provider_ledger or root / "provider-ledger.db").resolve(),
            "provider_registry_path": args.registry.resolve(),
            "database_url": URL.create("sqlite", database=str(root / "studio.db")),
            "cas_root": root / "cas",
            "work_root": root / "work",
            "media_scratch_root": settings.media_scratch_root or root / "scratch",
            "host": args.host,
            "port": args.port,
        }
    )
    serve(config)


if __name__ == "__main__":
    main()
