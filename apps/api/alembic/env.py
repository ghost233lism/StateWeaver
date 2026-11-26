from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool
from stateweaver.persistence.models import Base

config = context.config
connection = config.attributes.get("connection")
database_url = config.get_main_option("sqlalchemy.url")
if (connection is None or context.is_offline_mode()) and not database_url:
    raise RuntimeError("Use StateWeaver startup or stateweaver-db check to configure SQLite")
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run_migrations(connection: Connection) -> None:
    # SQLite does not implicitly start a transaction for CREATE TABLE.
    # Keep the whole revision atomic if initialization is interrupted.
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    context.configure(connection=connection, target_metadata=Base.metadata, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run_migrations(connection)
else:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.begin() as connection:
        run_migrations(connection)
