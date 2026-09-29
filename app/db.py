from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  — регистрирует таблицы в метаданных
from app.config import settings

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

_SQL_TYPES = {"INTEGER": "INTEGER", "BOOLEAN": "BOOLEAN", "VARCHAR": "VARCHAR", "DATETIME": "DATETIME"}


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Простая миграция SQLite: дописывает колонки, появившиеся в моделях."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                sql_type = _SQL_TYPES.get(str(column.type).split("(")[0], "VARCHAR")
                default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                clause = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {sql_type}"
                if default is not None:
                    clause += f" DEFAULT {int(default) if isinstance(default, bool) else repr(default)}"
                conn.execute(text(clause))


def get_session() -> Session:
    return Session(engine)
