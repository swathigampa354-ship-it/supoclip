import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Load environment variables
load_dotenv()


def normalize_database_url(url: str | None) -> str:
    """Return an asyncpg-compatible URL.

    Managed platforms (Render, Heroku, Railway, ...) hand out
    ``postgres://`` or ``postgresql://`` URLs, which SQLAlchemy maps to the
    psycopg2 driver. The backend is fully async, so rewrite those to the
    asyncpg driver instead of making operators edit dashboard values.
    """
    if not url:
        return DEFAULT_DATABASE_URL
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


# Optional schema isolation. Two SupoClip components (the Next.js/Prisma
# frontend and this backend) both own tables named users/tasks/sources, so a
# shared database needs separate schemas. Empty (the default) keeps the
# historical behaviour of using the connection's default schema.
DB_SCHEMA = os.getenv("DB_SCHEMA", "").strip()

DEFAULT_DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost:5432/supoclip")
)

_database_url_override: str | None = None
_engine_override: AsyncEngine | None = None
_session_maker_override: async_sessionmaker[AsyncSession] | None = None
_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


# Base class for all models
class Base(DeclarativeBase):
    pass


def _validated_schema() -> str:
    """DB_SCHEMA as a safe SQL identifier, or an empty string."""
    if not DB_SCHEMA:
        return ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DB_SCHEMA):
        raise ValueError(
            "DB_SCHEMA must be a plain Postgres identifier "
            "(letters, digits and underscores, not starting with a digit)"
        )
    return DB_SCHEMA


def _build_engine(database_url: str) -> AsyncEngine:
    server_settings = {"idle_in_transaction_session_timeout": "300000"}
    schema = _validated_schema()
    if schema:
        # Keep public in the path so extensions (uuid-ossp, pgcrypto) and
        # built-ins resolved from the default schema keep working.
        server_settings["search_path"] = f"{schema},public"

    return create_async_engine(
        database_url,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        # Safety net for the primary leak fix in api/routes/tasks.py
        # (get_clip_file/export_clip release their session before streaming):
        # any session parked mid-transaction this long is leaked, not legitimately
        # busy. Killing it returns the connection to the pool instead of letting
        # it accumulate toward QueuePool exhaustion.
        connect_args={"server_settings": server_settings},
    )


def get_database_url() -> str:
    if _database_url_override:
        return normalize_database_url(_database_url_override)
    return normalize_database_url(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


def configure_database(
    *,
    database_url: str | None = None,
    engine: AsyncEngine | None = None,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    global _database_url_override, _engine_override, _session_maker_override
    _database_url_override = database_url
    _engine_override = engine
    _session_maker_override = session_maker


def get_engine() -> AsyncEngine:
    global _engine
    if _engine_override is not None:
        return _engine_override
    if _engine is None:
        _engine = _build_engine(get_database_url())
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker_override is not None:
        return _session_maker_override
    if _session_maker is None:
        _session_maker = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_maker


def AsyncSessionLocal() -> AsyncSession:
    return get_session_maker()()


# Dependency to get database session
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# Initialize database
async def init_db():
    async with get_engine().begin() as conn:
        schema = _validated_schema()
        if schema:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )

        migrations_dir = Path(__file__).parent / "migrations" / "sql"
        if migrations_dir.exists():
            files = sorted([p for p in migrations_dir.glob("*.sql") if p.is_file()])
            for migration_file in files:
                version = migration_file.name
                already_applied = await conn.execute(
                    text(
                        "SELECT 1 FROM schema_migrations WHERE version = :version LIMIT 1"
                    ),
                    {"version": version},
                )
                if already_applied.scalar() is not None:
                    continue

                sql = migration_file.read_text()
                # asyncpg doesn't support multiple statements in one execute(),
                # so split on semicolons and run each statement individually
                for statement in sql.split(";"):
                    statement = statement.strip()
                    if statement:
                        await conn.execute(text(statement))
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                    {"version": version},
                )


# Close database connections
async def close_db():
    global _engine, _session_maker
    engine = _engine_override or _engine
    if engine is not None:
        await engine.dispose()
    _engine = None
    _session_maker = None


async def reset_database_state() -> None:
    global _database_url_override, _engine_override, _session_maker_override
    await close_db()
    _database_url_override = None
    _engine_override = None
    _session_maker_override = None
