from alembic import context
from sqlalchemy import create_engine, pool

from trellmark import config as trellmark_config

config = context.config
target_metadata = None


def database_url():
    return trellmark_config.database_url()


def run_migrations_offline():
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    # The DSN comes from trellmark.config, not from alembic.ini, so a password
    # never has to be written into a tracked file or escaped for ConfigParser.
    connectable = create_engine(
        database_url(),
        poolclass=pool.NullPool,
        hide_parameters=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
