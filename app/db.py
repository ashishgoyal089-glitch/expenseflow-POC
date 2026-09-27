"""Database engine, session factory, declarative base and FastAPI session dependency."""

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///expenseflow.db")

# check_same_thread=False: FastAPI may run a request's sync code on a different thread
# from the one that opened the connection. Each request still gets its own session.
engine: Engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False
)


class Base(DeclarativeBase):
    """Declarative base that all ORM models inherit from."""


def get_db() -> Iterator[Session]:
    """Yield one session per request and always close it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables that don't exist yet. Existing tables are left unchanged."""
    import app.models  # noqa: F401  (registers the models on Base.metadata)

    Base.metadata.create_all(bind=engine)
