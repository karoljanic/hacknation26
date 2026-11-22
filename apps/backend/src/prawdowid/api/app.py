import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from prawdowid.api.admin import router as admin_router
from prawdowid.api.check import router as check_router
from prawdowid.api.verify import router as verify_router
from prawdowid.api.x_auth import router as x_auth_router
from prawdowid.persistence.db import get_session
from prawdowid.security.injection_guard import get_injection_guard

log = logging.getLogger(__name__)


async def _preload_injection_guard() -> None:
    try:
        await asyncio.to_thread(get_injection_guard)
    except Exception:
        log.exception("Injection guard failed to load at startup; /check will reject requests until it loads")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Load the injection guard's local models in the background so the first
    # request does not pay for it and /health is up meanwhile; a request that
    # arrives earlier waits for the same load. A failure is not fatal here:
    # /check retries the load and fails closed until it succeeds.
    preload = asyncio.create_task(_preload_injection_guard())
    yield
    preload.cancel()


app = FastAPI(title="Prawdowid API", version="0.1.0", lifespan=lifespan)
app.include_router(check_router)
app.include_router(verify_router)
app.include_router(x_auth_router)
app.include_router(admin_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness of the API process only."""
    return {"status": "ok"}


@app.get("/health/ready")
def ready(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    """Readiness: the database is reachable and migrated."""
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok", "db_revision": revision}