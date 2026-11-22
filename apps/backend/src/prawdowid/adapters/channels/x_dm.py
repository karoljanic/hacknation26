"""X (Twitter) direct messages for the @prawdowid account: OAuth 2.0 user token
refresh, reading new DM events and replying.

Tokens live in the file written by ``api.x_auth`` (0600). X access tokens
last 2 hours; the refresh token rotates on every refresh, so the new pair is
saved atomically before it is used.
"""

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from prawdowid.api.x_auth import TOKEN_FILE, StoredAuthorization, TokenResponse

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
# Refresh a little before expiry so a request never carries a dead token.
REFRESH_MARGIN = timedelta(minutes=5)
MAX_RESPONSE_BYTES = 2_000_000
# X DM text limit.
MAX_DM_CHARS = 10_000
_token_lock = threading.Lock()


class XApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DMEvent(BaseModel):
    id: str
    event_type: str
    text: str = ""
    sender_id: str | None = None
    dm_conversation_id: str | None = None
    created_at: datetime | None = None
    media_keys: list[str] = Field(default_factory=list)
    # (t.co link, expanded URL) pairs from the message text.
    urls: list[tuple[str, str]] = Field(default_factory=list)
    referenced_tweet_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Media:
    media_key: str
    type: str
    url: str | None


@dataclass(frozen=True)
class Post:
    """A post shared in the DM, as X returns it."""

    id: str
    text: str
    author: str | None
    created_at: datetime | None = None


@dataclass(frozen=True)
class DMBatch:
    events: list[DMEvent]
    media: dict[str, Media]
    posts: dict[str, Post]


def _client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("X_CLIENT_ID", "").strip()
    client_secret = os.environ.get("X_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise XApiError("X_CLIENT_ID and X_CLIENT_SECRET are required to refresh the X token")
    return client_id, client_secret


def _save(token: TokenResponse, previous: StoredAuthorization, path: Path) -> StoredAuthorization:
    stored = StoredAuthorization(
        account=previous.account,
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        token_type=token.token_type,
        scope=token.scope,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=token.expires_in),
    )
    data = {
        **stored.model_dump(mode="json", exclude={"access_token", "refresh_token"}),
        "access_token": token.access_token.get_secret_value(),
        "refresh_token": token.refresh_token.get_secret_value(),
    }
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    return stored


def parse_events(body: dict[str, Any]) -> DMBatch:
    includes = body.get("includes") or {}
    events = [
        DMEvent(
            id=raw["id"],
            event_type=raw.get("event_type", ""),
            text=raw.get("text", ""),
            sender_id=raw.get("sender_id"),
            dm_conversation_id=raw.get("dm_conversation_id"),
            created_at=raw.get("created_at"),
            media_keys=(raw.get("attachments") or {}).get("media_keys", []),
            urls=[
                (url["url"], url.get("expanded_url") or url["url"])
                for url in (raw.get("entities") or {}).get("urls", [])
                if url.get("url")
            ],
            referenced_tweet_ids=[ref["id"] for ref in raw.get("referenced_tweets", []) if ref.get("id")],
        )
        for raw in body.get("data", [])
    ]
    media = {
        item["media_key"]: Media(item["media_key"], item.get("type", ""), item.get("url"))
        for item in includes.get("media", [])
    }
    usernames = {user["id"]: user.get("username") for user in includes.get("users", [])}
    posts = {
        tweet["id"]: Post(
            tweet["id"], tweet.get("text", ""), usernames.get(tweet.get("author_id", "")),
            datetime.fromisoformat(tweet["created_at"].replace("Z", "+00:00")) if tweet.get("created_at") else None,
        )
        for tweet in includes.get("tweets", [])
    }
    return DMBatch(events, media, posts)


class XDirectMessages:
    def __init__(self, client: httpx.Client, *, token_file: Path = TOKEN_FILE) -> None:
        self.client = client
        self.token_file = token_file

    @property
    def account_id(self) -> str:
        return self._stored().account.id

    def _stored(self) -> StoredAuthorization:
        if not self.token_file.exists():
            raise XApiError("@prawdowid is not authorized: open /auth/x first")
        return StoredAuthorization.model_validate_json(self.token_file.read_bytes())

    def _access_token(self) -> str:
        with _token_lock:
            stored = self._stored()
            if stored.expires_at - REFRESH_MARGIN > datetime.now(timezone.utc):
                return stored.access_token.get_secret_value()
            client_id, client_secret = _client_credentials()
            response = self.client.post(
                f"{API}/oauth2/token",
                auth=httpx.BasicAuth(client_id, client_secret),
                data={"grant_type": "refresh_token", "refresh_token": stored.refresh_token.get_secret_value()},
            )
            self._check(response, "token refresh")
            refreshed = _save(TokenResponse.model_validate_json(response.content), stored, self.token_file)
            log.info("X access token refreshed, valid until %s", refreshed.expires_at.isoformat())
            return refreshed.access_token.get_secret_value()

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise XApiError(f"X {what}: response too large")
        if not response.is_success:
            # The body may explain the access tier problem; it never holds tokens.
            raise XApiError(f"X {what}: HTTP {response.status_code} {response.text[:300]}", response.status_code)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self.client.request(
            method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, **kwargs
        )
        self._check(response, f"{method} {url.removeprefix(API)}")
        return response

    def recent_events(self, max_results: int = 50) -> DMBatch:
        """Newest DM events of the account (sent and received), newest first,
        with attached images and the text of shared posts."""
        response = self._request(
            "GET",
            f"{API}/dm_events",
            params={
                "max_results": max_results,
                "event_types": "MessageCreate",
                "dm_event.fields": (
                    "id,event_type,text,sender_id,dm_conversation_id,created_at,attachments,entities,referenced_tweets"
                ),
                "expansions": "attachments.media_keys,referenced_tweets.id,referenced_tweets.id.author_id",
                "media.fields": "media_key,type,url",
                "tweet.fields": "id,text,author_id,created_at",
                "user.fields": "username",
            },
        )
        return parse_events(response.json())

    def download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """An image attached to a DM (served from X's own media hosts)."""
        host = httpx.URL(url).host
        if not (host == "ton.twitter.com" or host.endswith(".twimg.com")):
            raise XApiError(f"Unexpected DM media host {host}")
        response = self.client.get(url, headers={"Authorization": f"Bearer {self._access_token()}"})
        self._check(response, "media download")
        if len(response.content) > max_bytes:
            raise XApiError("DM media too large")
        return response.content, response.headers.get("content-type", "").split(";")[0].strip().lower()

    def reply(self, participant_id: str, text: str) -> None:
        self._request(
            "POST",
            f"{API}/dm_conversations/with/{participant_id}/messages",
            json={"text": text[:MAX_DM_CHARS]},
        )
