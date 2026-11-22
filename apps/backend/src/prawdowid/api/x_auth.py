import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr, ValidationError

router = APIRouter(prefix="/auth/x", tags=["X authorization"])
log = logging.getLogger(__name__)
TOKEN_FILE = Path.home() / ".local/share/prawdowid/x-oauth.json"
SCOPES = {"dm.read", "dm.write", "tweet.read", "users.read", "offline.access"}
COOKIE = "prawdowid_x_oauth_state"
STATE_TTL = 600


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str


@dataclass(frozen=True)
class PendingAuthorization:
    verifier: str = field(repr=False)
    expires_at: float
    settings: Settings


_pending: dict[str, PendingAuthorization] = {}
_pending_lock = threading.Lock()


class TokenResponse(BaseModel):
    access_token: SecretStr = Field(min_length=1)
    refresh_token: SecretStr = Field(min_length=1)
    token_type: Literal["bearer"]
    expires_in: int = Field(gt=0, le=31_536_000)
    scope: str


class Account(BaseModel):
    id: str = Field(min_length=1)
    username: str = Field(min_length=1)


class AccountResponse(BaseModel):
    data: Account


class StoredAuthorization(BaseModel):
    account: Account
    access_token: SecretStr
    refresh_token: SecretStr
    token_type: Literal["bearer"]
    scope: str
    expires_at: datetime


class AuthorizationStatus(BaseModel):
    authorized: bool
    username: str | None = None
    expires_at: datetime | None = None
    access_token_expired: bool | None = None


def get_settings() -> Settings:
    values = [os.environ.get(name, "").strip() for name in (
        "X_CLIENT_ID", "X_CLIENT_SECRET", "X_REDIRECT_URI"
    )]
    if not all(values):
        raise HTTPException(503, "Uzupełnij X_CLIENT_ID, X_CLIENT_SECRET i X_REDIRECT_URI w .env.")
    client_id, client_secret, redirect_uri = values
    parsed = urlsplit(redirect_uri)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.path != "/auth/x/callback"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(503, "X_REDIRECT_URI musi wskazywać lokalny /auth/x/callback.")
    return Settings(client_id, client_secret, redirect_uri)


def get_http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        yield client


def check_response(response: httpx.Response) -> None:
    if not response.is_success:
        log.warning("X authorization request failed: HTTP %s", response.status_code)
        raise HTTPException(502, f"X API zwróciło HTTP {response.status_code}. Spróbuj autoryzacji ponownie.")
    if len(response.content) > 64_000:
        raise HTTPException(502, "Odpowiedź X API przekracza limit rozmiaru.")


def save_authorization(token: TokenResponse, account: Account) -> None:
    data = {
        "account": account.model_dump(),
        "access_token": token.access_token.get_secret_value(),
        "refresh_token": token.refresh_token.get_secret_value(),
        "token_type": token.token_type,
        "scope": token.scope,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)).isoformat(),
    }
    temporary: Path | None = None
    try:
        TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=TOKEN_FILE.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, TOKEN_FILE)
        temporary = None
    except OSError as exc:
        log.error("Unable to persist X authorization")
        raise HTTPException(500, "Nie udało się bezpiecznie zapisać tokenów X.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@router.get("", response_class=HTMLResponse)
def authorization_page() -> str:
    return """<!doctype html><html lang="pl"><meta charset="utf-8">
<title>Prawdowid — autoryzacja X</title><h1>Podłącz konto @prawdowid</h1>
<p>Otwórz tę stronę w profilu przeglądarki zalogowanym na X jako @prawdowid.</p>
<p>Zgoda obejmuje odczyt i wysyłanie DM. Tokeny zostaną zapisane lokalnie.</p>
<p><a href="/auth/x/start">Autoryzuj @prawdowid na X</a></p>
<p>To tylko podłączenie konta — automatyczny odbiór i odpowiedzi na DM nie są jeszcze uruchomione.</p>
</html>"""


@router.get("/start")
def start_authorization(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> RedirectResponse:
    callback = urlsplit(settings.redirect_uri)
    if request.url.netloc != callback.netloc or request.url.scheme != callback.scheme:
        raise HTTPException(400, "Otwórz /auth/x na tym samym adresie i porcie co X_REDIRECT_URI.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.monotonic()
    with _pending_lock:
        for expired in [key for key, value in _pending.items() if value.expires_at <= now]:
            del _pending[expired]
        if len(_pending) >= 32:
            raise HTTPException(429, "Zbyt wiele rozpoczętych autoryzacji. Spróbuj za 10 minut.")
        _pending[state] = PendingAuthorization(verifier, now + STATE_TTL, settings)
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": " ".join(sorted(SCOPES)),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse("https://x.com/i/oauth2/authorize?" + urlencode(params), status_code=302)
    response.set_cookie(
        COOKIE, state, max_age=STATE_TTL, httponly=True, samesite="lax",
        secure=callback.scheme == "https", path="/auth/x",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/callback", response_class=HTMLResponse)
def authorization_callback(
    request: Request,
    client: Annotated[httpx.Client, Depends(get_http_client)],
    state: Annotated[str, Query(min_length=1, max_length=256)],
    code: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> HTMLResponse:
    cookie = request.cookies.get(COOKIE)
    if cookie is None or not secrets.compare_digest(cookie, state):
        raise HTTPException(400, "Nieprawidłowa sesja autoryzacji. Zacznij ponownie z /auth/x.")
    with _pending_lock:
        pending = _pending.pop(state, None)
    if pending is None or pending.expires_at <= time.monotonic():
        raise HTTPException(400, "Autoryzacja wygasła lub została już wykorzystana. Zacznij ponownie.")
    if error is not None or not code:
        raise HTTPException(400, "Nie udzielono zgody na dostęp do konta X.")
    try:
        response = client.post(
            "https://api.x.com/2/oauth2/token",
            auth=httpx.BasicAuth(pending.settings.client_id, pending.settings.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending.settings.redirect_uri,
                "code_verifier": pending.verifier,
            },
        )
        check_response(response)
        try:
            token = TokenResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise HTTPException(502, "X zwróciło nieprawidłową odpowiedź autoryzacji.") from exc
        if not SCOPES.issubset(set(token.scope.split())):
            raise HTTPException(403, "Nie przyznano wszystkich wymaganych uprawnień do DM i odświeżania tokenów.")
        response = client.get(
            "https://api.x.com/2/users/me",
            headers={"Authorization": f"Bearer {token.access_token.get_secret_value()}"},
        )
        check_response(response)
        try:
            account = AccountResponse.model_validate_json(response.content).data
        except ValidationError as exc:
            raise HTTPException(502, "X zwróciło nieprawidłowe dane konta.") from exc
    except httpx.RequestError as exc:
        log.error("Unable to connect to X during authorization")
        raise HTTPException(502, "Nie udało się połączyć z X. Spróbuj autoryzacji ponownie.") from exc
    if account.username.casefold() != "prawdowid":
        raise HTTPException(403, "Zatwierdzono inne konto. Zaloguj się jako @prawdowid i zacznij ponownie.")
    save_authorization(token, account)
    result = HTMLResponse(
        '<!doctype html><html lang="pl"><meta charset="utf-8">'
        "<h1>Autoryzowano @prawdowid</h1><p>Tokeny zapisano lokalnie.</p>"
        "<p>Automatyczny odbiór i odpowiedzi na DM nie są jeszcze uruchomione.</p></html>"
    )
    result.delete_cookie(COOKIE, path="/auth/x")
    result.headers["Cache-Control"] = "no-store"
    result.headers["Referrer-Policy"] = "no-referrer"
    return result


@router.get("/status", response_model=AuthorizationStatus)
def authorization_status() -> AuthorizationStatus:
    if not TOKEN_FILE.exists():
        return AuthorizationStatus(authorized=False)
    try:
        stored = StoredAuthorization.model_validate_json(TOKEN_FILE.read_bytes())
        expired = stored.expires_at <= datetime.now(timezone.utc)
    except (OSError, ValidationError, TypeError) as exc:
        log.error("Unable to read stored X authorization")
        raise HTTPException(500, "Nie udało się odczytać zapisanej autoryzacji X.") from exc
    return AuthorizationStatus(
        authorized=True, username=stored.account.username,
        expires_at=stored.expires_at, access_token_expired=expired,
    )
