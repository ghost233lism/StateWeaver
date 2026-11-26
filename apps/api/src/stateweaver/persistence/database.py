from __future__ import annotations

import asyncio
import fcntl
import logging
import sqlite3
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, event, inspect
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)
_alembic_context_lock = RLock()


def sqlite_database_path(database_url: str | URL) -> tuple[Path | None, bool]:
    """Resolve the SQLite filename and whether the connection uses a URI."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None, False
    if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        raise ValueError("Studio supports only the synchronous SQLite driver")
    args, options = SQLiteDialect_pysqlite().create_connect_args(url)
    filename = str(args[0])
    uri = bool(options.get("uri", False))
    if uri and filename.startswith("file:"):
        parsed = urlsplit(filename)
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("Studio SQLite file URIs must use a local path")
        filename = unquote(parsed.path)
        if parse_qs(parsed.query).get("mode") == ["memory"]:
            return None, uri
    if filename in {"", ":memory:"}:
        return None, uri
    return Path(filename).resolve(), uri


@contextmanager
def _schema_file_lock(database_url: str | URL) -> Iterator[None]:
    path, _ = sqlite_database_path(database_url)
    if path is None:
        yield
        return
    lock = path.with_name(path.name + ".schema.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def is_sqlite_contention(error: BaseException) -> bool:
    """Only SQLite BUSY/LOCKED, including SQLAlchemy's driver wrapper."""
    original = error.orig if isinstance(error, SAOperationalError) else error
    if not isinstance(original, sqlite3.OperationalError):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(original) in {"database is locked", "database table is locked"}


def control_error_diagnostic(error: BaseException) -> dict[str, Any]:
    """Keep canonical codes and stack locations without private exception contents."""
    original = getattr(error, "orig", error)
    return {
        "exception_type": type(error).__name__,
        "sqlite_errorcode": getattr(original, "sqlite_errorcode", None),
        "sqlite_errorname": getattr(original, "sqlite_errorname", None),
        "stack": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)[-20:]
        ],
    }


async def sqlite_control_io[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Retry atomic control/database work, never a provider submission."""
    waiting = False
    while True:
        try:
            value = await asyncio.to_thread(function, *args, **kwargs)
            if waiting:
                logger.info("Native control resumed (%s)", getattr(function, "__name__", "control"))
            return value
        except Exception as error:
            if not is_sqlite_contention(error):
                raise
            if not waiting:
                logger.warning(
                    "Native control waiting for SQLite (%s)",
                    getattr(function, "__name__", "control"),
                )
                waiting = True
            # Retrying the original operation rechecks its owner/generation.
            # Cancellation and real fence loss remain terminal for this waiter.
            await asyncio.sleep(0.25)


class Database:
    """SQLite database wrapper with Studio's connection and transaction settings."""

    def __init__(self, url: str | URL) -> None:
        self.url = url
        self.write_lock = RLock()
        db_path, uri = sqlite_database_path(url)
        if db_path is not None and not uri:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, future=True)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.sessions = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )

    @staticmethod
    def _configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def create_schema(self) -> None:
        """Create the current schema for isolated tests, never for application startup."""

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.sessions()
        try:
            with session.begin():
                yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class SchemaStateError(RuntimeError):
    """The database cannot be safely used or advanced by the schema lifecycle."""


@dataclass(frozen=True, slots=True)
class SchemaState:
    current_heads: tuple[str, ...]
    expected_heads: tuple[str, ...]
    application_tables: tuple[str, ...]
    drift: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.application_tables and not self.current_heads

    @property
    def is_unversioned(self) -> bool:
        return bool(self.application_tables) and not self.current_heads

    @property
    def is_current(self) -> bool:
        return self.current_heads == self.expected_heads and not self.drift


def inspect_schema(database: Database) -> SchemaState:
    """Inspect migration provenance and model drift without changing application tables."""

    config = _alembic_config()
    expected_heads = tuple(sorted(ScriptDirectory.from_config(config).get_heads()))
    with database.engine.connect() as connection:
        migration_context = MigrationContext.configure(connection)
        current_heads = tuple(sorted(migration_context.get_current_heads()))
        tables = tuple(
            sorted(
                table
                for table in inspect(connection).get_table_names()
                if table != migration_context.version_table
            )
        )
        drift = _schema_drift(connection)
    return SchemaState(
        current_heads=current_heads,
        expected_heads=expected_heads,
        application_tables=tables,
        drift=drift,
    )


def verify_schema_current(database: Database) -> SchemaState:
    """Fail closed unless the database is at the migration head with no model drift."""

    state = inspect_schema(database)
    if not state.expected_heads:
        raise SchemaStateError("the migration repository has no head revision")
    if state.is_empty:
        raise SchemaStateError("StateWeaver schema has not been initialized")
    if state.current_heads != state.expected_heads:
        raise SchemaStateError("StateWeaver database does not match the current release schema")
    if state.drift:
        raise SchemaStateError(
            f"database schema differs from the current model ({len(state.drift)} change(s))"
        )
    return state


def upgrade_schema(database: Database) -> SchemaState:
    """Initialize the release schema or advance a database from a supported revision."""

    # Hold the schema lock through verification to serialize concurrent starts.
    with _schema_file_lock(database.url):
        state = inspect_schema(database)
        if not state.expected_heads:
            raise SchemaStateError("the migration repository has no head revision")
        if state.is_unversioned:
            raise SchemaStateError(
                "Previous or unversioned database rejected; use a new StateWeaver data directory"
            )
        _reject_unknown_revisions(state)
        with _alembic_command_config(database) as config:
            command.upgrade(config, "head")
        return verify_schema_current(database)


def verify_migration_history(*, temporary_root: Path | None = None) -> SchemaState:
    """Validate every migration and model metadata against a disposable SQLite database."""

    with TemporaryDirectory(prefix="stateweaver-schema-check-", dir=temporary_root) as directory:
        database = Database(URL.create("sqlite", database=str(Path(directory) / "schema.db")))
        try:
            upgrade_schema(database)
            state = verify_schema_current(database)
            with _alembic_command_config(database) as config:
                command.check(config)
            return state
        finally:
            database.engine.dispose()


def _schema_drift(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": False},
    )
    changes = compare_metadata(context, Base.metadata)
    return tuple(_describe_schema_change(change) for change in changes)


def _describe_schema_change(change: Any) -> str:
    if isinstance(change, tuple) and change:
        return str(change[0])
    if isinstance(change, list):
        return "constraint_change"
    return type(change).__name__


def _reject_unknown_revisions(state: SchemaState) -> None:
    if not state.current_heads:
        return
    known_revisions = {
        revision.revision
        for revision in ScriptDirectory.from_config(_alembic_config()).walk_revisions()
    }
    unknown = sorted(set(state.current_heads) - known_revisions)
    if unknown:
        raise SchemaStateError(
            "Previous or unknown database revision; use a new StateWeaver data directory"
        )


def _alembic_config() -> Config:
    api_root = Path(__file__).resolve().parents[3]
    return Config(str(api_root / "alembic.ini"))


@contextmanager
def _alembic_command_config(database: Database) -> Iterator[Config]:
    # Alembic's environment proxy is process-wide.
    with _alembic_context_lock, database.engine.begin() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        yield config
