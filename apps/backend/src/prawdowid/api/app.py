import asyncio
import logging
import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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


def _x_dm_bot_interval() -> float:
    raw = os.environ.get("X_DM_POLL_SECONDS", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        log.error("X_DM_POLL_SECONDS must be a number of seconds, got %r; X DM bot disabled", raw)
        return 0.0


async def _run_x_dm_bot(interval: float) -> None:
    from prawdowid.adapters.channels.x_dm import XDirectMessages
    from prawdowid.api.dependencies import build_pipeline
    from prawdowid.api.rate_limit import get_check_guard
    from prawdowid.api.x_auth import TOKEN_FILE
    from prawdowid.services.x_bot import XDmBot, run_forever

    if not TOKEN_FILE.exists():
        log.warning("X_DM_POLL_SECONDS is set, but @prawdowid is not authorized (open /auth/x); X DM bot disabled")
        return
    ca_bundle = os.environ.get("SSL_CERT_FILE")
    verify: bool | ssl.SSLContext = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
    with httpx.Client(timeout=20, follow_redirects=False, verify=verify) as client:
        bot = XDmBot(
            XDirectMessages(client),
            pipeline_factory=build_pipeline,
            injection_guard=get_injection_guard,
            limits=get_check_guard(),
            state_file=TOKEN_FILE.parent / "x-dm-state.json",
        )
        await run_forever(bot, interval)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Load the injection guard's local models in the background so the first
    # request does not pay for it and /health is up meanwhile; a request that
    # arrives earlier waits for the same load. A failure is not fatal here:
    # /check retries the load and fails closed until it succeeds.
    preload = asyncio.create_task(_preload_injection_guard())
    interval = _x_dm_bot_interval()
    x_bot = asyncio.create_task(_run_x_dm_bot(interval)) if interval else None
    yield
    preload.cancel()
    if x_bot is not None:
        x_bot.cancel()


app = FastAPI(title="Prawdowid API", version="0.1.0", lifespan=lifespan)
app.include_router(check_router)
app.include_router(verify_router)
app.include_router(x_auth_router)


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