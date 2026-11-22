from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from prawdowid.api.check import router as check_router
from prawdowid.api.verify import router as verify_router
from prawdowid.api.x_auth import router as x_auth_router
from prawdowid.persistence.db import get_session

app = FastAPI(title="Prawdowid API", version="0.1.0")
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