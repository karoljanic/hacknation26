import base64
import hashlib
import json
import stat
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from prawdowid.api import x_auth
from prawdowid.api.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "test-client")
    monkeypatch.setenv("X_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("X_REDIRECT_URI", "http://testserver/auth/x/callback")
    monkeypatch.setattr(x_auth, "TOKEN_FILE", tmp_path / "tokens" / "x-oauth.json")
    app.dependency_overrides[x_auth.get_settings] = lambda: x_auth.Settings(
        "test-client", "test-secret", "http://testserver/auth/x/callback"
    )
    x_auth._pending.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    x_auth._pending.clear()


def begin(client):
    response = client.get("/auth/x/start", follow_redirects=False)
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)


def mock_x(handler):
    def dependency():
        with httpx.Client(transport=httpx.MockTransport(handler)) as mocked:
            yield mocked
    app.dependency_overrides[x_auth.get_http_client] = dependency


def token_response():
    return {
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "token_type": "bearer",
        "expires_in": 7200,
        "scope": " ".join(sorted(x_auth.SCOPES)),
    }


def test_pkce_exchange_persists_only_prawdowid_and_status_hides_tokens(client):
    params = begin(client)
    assert params["code_challenge_method"] == ["S256"]
    assert set(params["scope"][0].split()) == x_auth.SCOPES
    state = params["state"][0]
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/2/oauth2/token":
            assert request.headers["authorization"] == "Basic " + base64.b64encode(
                b"test-client:test-secret"
            ).decode()
            body = parse_qs(request.content.decode())
            assert body["code"] == ["test-code"]
            assert body["redirect_uri"] == params["redirect_uri"]
            assert body["grant_type"] == ["authorization_code"]
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(body["code_verifier"][0].encode()).digest()
            ).rstrip(b"=").decode()
            assert challenge == params["code_challenge"][0]
            return httpx.Response(200, json=token_response())
        assert request.headers["authorization"] == "Bearer test-access-token"
        return httpx.Response(200, json={"data": {"id": "123", "username": "Prawdowid"}})

    mock_x(handle)
    response = client.get("/auth/x/callback", params={"state": state, "code": "test-code"})
    assert response.status_code == 200
    assert "test-access-token" not in response.text
    assert calls == ["/2/oauth2/token", "/2/users/me"]
    stored = json.loads(x_auth.TOKEN_FILE.read_text())
    assert stored["access_token"] == "test-access-token"
    assert stored["refresh_token"] == "test-refresh-token"
    assert stat.S_IMODE(x_auth.TOKEN_FILE.stat().st_mode) == 0o600
    status = client.get("/auth/x/status")
    assert status.json()["username"] == "Prawdowid"
    assert status.json()["access_token_expired"] is False
    assert "token" not in status.text.replace("access_token_expired", "")
    assert response.headers["cache-control"] == "no-store"
    replay = client.get("/auth/x/callback", params={"state": state, "code": "test-code"})
    assert replay.status_code == 400
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["wrong-cookie", "missing-cookie", "expired", "denied"])
def test_invalid_authorization_never_calls_x(client, failure):
    state = begin(client)["state"][0]
    if failure == "wrong-cookie":
        state = "attacker-state"
    elif failure == "missing-cookie":
        client.cookies.clear()
    elif failure == "expired":
        x_auth._pending[state] = replace(x_auth._pending[state], expires_at=time.monotonic() - 1)
    mock_x(lambda request: pytest.fail("Invalid authorization must not contact X"))
    params = {"state": state, "code": "test-code"}
    if failure == "denied":
        params["error"] = "access_denied"
    response = client.get("/auth/x/callback", params=params)
    assert response.status_code == 400
    assert not x_auth.TOKEN_FILE.exists()


@pytest.mark.parametrize("failure", ["wrong-account", "missing-scope", "invalid-token", "http-error", "network-error"])
def test_failed_exchange_does_not_replace_existing_authorization(client, failure):
    x_auth.TOKEN_FILE.parent.mkdir()
    x_auth.TOKEN_FILE.write_text("previous-authorization")
    state = begin(client)["state"][0]

    def handle(request):
        if request.url.path == "/2/oauth2/token":
            if failure == "http-error":
                return httpx.Response(401, text="SECRET ERROR BODY")
            if failure == "network-error":
                raise httpx.ConnectError("SECRET TRANSPORT DETAIL", request=request)
            if failure == "invalid-token":
                return httpx.Response(200, json={"access_token": "SECRET TOKEN"})
            token = token_response()
            if failure == "missing-scope":
                token["scope"] = "users.read"
            return httpx.Response(200, json=token)
        return httpx.Response(200, json={"data": {"id": "456", "username": "JanicKarol"}})

    mock_x(handle)
    response = client.get("/auth/x/callback", params={"state": state, "code": "test-code"})
    assert response.status_code in {403, 502}
    assert "SECRET" not in response.text
    assert x_auth.TOKEN_FILE.read_text() == "previous-authorization"


def test_storage_failure_is_not_reported_as_success(client, monkeypatch):
    state = begin(client)["state"][0]
    mock_x(lambda request: httpx.Response(
        200, json=token_response() if request.url.path.endswith("/token")
        else {"data": {"id": "123", "username": "prawdowid"}},
    ))

    def fail_replace(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(x_auth.os, "replace", fail_replace)
    response = client.get("/auth/x/callback", params={"state": state, "code": "test-code"})
    assert response.status_code == 500
    assert list(x_auth.TOKEN_FILE.parent.iterdir()) == []


def test_configuration_is_opt_in_and_callback_must_be_local(client, monkeypatch):
    app.dependency_overrides.pop(x_auth.get_settings)
    monkeypatch.delenv("X_CLIENT_SECRET")
    assert client.get("/health").status_code == 200
    assert client.get("/auth/x/start", follow_redirects=False).status_code == 503
    monkeypatch.setenv("X_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("X_REDIRECT_URI", "https://attacker.example/auth/x/callback")
    assert client.get("/auth/x/start", follow_redirects=False).status_code == 503
    assert client.get("/auth/x/status").json() == {
        "authorized": False, "username": None, "expires_at": None, "access_token_expired": None,
    }
