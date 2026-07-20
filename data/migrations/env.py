"""Alembic environment.

Connection details come from ``shared/config.py`` — i.e. ultimately from the
Pulumi stack via ``infra/env-from-stack.py`` — rather than from ``alembic.ini``.
That keeps one source of truth for where the database is and means no password
is ever written into a committed file.

⚠️ There is no ``target_metadata`` and autogenerate is not available. The
project uses psycopg with raw SQL and JSONB rather than an ORM, so there are no
SQLAlchemy models to diff against. Migrations are written by hand — which is
also why each one carries a comment explaining *why*, not just what.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from shared.config import Config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", Config.from_env().sqlalchemy_url)

# No ORM models — see the module docstring.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
