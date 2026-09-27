"""FastAPI application: loads .env, creates tables on startup and mounts the routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before importing app.db, which reads DATABASE_URL at import time.
load_dotenv()

from fastapi import FastAPI  # noqa: E402

from app.db import init_db  # noqa: E402
from app.routes import router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create any missing tables on startup."""
    init_db()
    yield


app: FastAPI = FastAPI(title="ExpenseFlow API", lifespan=lifespan)
app.include_router(router)
