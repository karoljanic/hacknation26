import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, TextContent
from pydantic import ValidationError

from prawdowid.adapters.sources.euvsdisinfo import (
    EUvsDisinfoAdapter,
    EUvsDisinfoRPCError,
    EUvsDisinfoToolError,
)
from prawdowid.domain.euvsdisinfo import (
    EUvsDisinfoConnection,
    EUvsDisinfoDateRange,
    EUvsDisinfoPageRequest,
    EUvsDisinfoSearchRequest,
)


class MCPServer:
    def __init__(self, *, sse: bool) -> None:
        self.sse = sse
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        headers = {}
        if method == "initialize":
            assert "mcp-session-id" not in request.headers
            headers["mcp-session-id"] = "server-issued-session"
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "euvsdisinfo-test", "version": "1.0"},
            }
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ["articles", "cases", "search", "narratives"]
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "Untrusted source text."}],
                "structuredContent": {
                    "cases": [{"id": "case-1", "claim": "Claim.", "fact": "Source analysis."}],
                    "total": 1,
                    "hasNextPage": False,
                },
            }
        else:
            raise AssertionError(f"Unexpected RPC method {method}")
        envelope = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            headers["Content-Type"] = "text/event-stream"
            return httpx.Response(
                200, headers=headers, content=f"event: message\ndata: {json.dumps(envelope)}\n\n"
            )
        return httpx.Response(200, headers=headers, json=envelope)


@pytest.mark.parametrize("mode", ["stateful", "stateless"])
@pytest.mark.parametrize("sse", [False, True])
def test_real_sdk_supports_both_session_modes_and_json_or_sse(mode: str, sse: bool) -> None:
    server = MCPServer(sse=sse)
    connection = EUvsDisinfoConnection(api_key="test-key", mode=mode)

    async def run() -> None:
        async with EUvsDisinfoAdapter.connect(
            connection, transport=httpx.MockTransport(server.handle)
        ) as source:
            tools = await source.list_tools()
            assert [tool.name for tool in tools.tools] == ["articles", "cases", "search", "narratives"]
            result = await source.cases(EUvsDisinfoPageRequest(limit=1))
            assert result.structured_content["cases"][0]["fact"] == "Source analysis."
            assert result.fetched_at.tzinfo is not None

    asyncio.run(run())
    posts = [request for request in server.requests if request.method == "POST"]
    methods = [json.loads(request.content)["method"] for request in posts]
    assert all(request.headers["Authorization"] == "Bearer test-key" for request in posts)
    assert all("test-key" not in str(request.url) for request in posts)
    assert all(
        {"application/json", "text/event-stream"} <= set(request.headers["Accept"].split(", "))
        for request in posts
    )
    if mode == "stateful":
        assert methods[:2] == ["initialize", "notifications/initialized"]
        assert all(
            request.headers["mcp-session-id"] == "server-issued-session"
            for request in posts[1:]
        )
        assert any(request.method == "DELETE" for request in server.requests)
    else:
        assert "initialize" not in methods and "notifications/initialized" not in methods
        session_ids = {request.headers["mcp-session-id"] for request in posts}
        assert len(session_ids) == 1 and "server-issued-session" not in session_ids
        assert not any(request.method == "DELETE" for request in server.requests)


@pytest.mark.parametrize("auth", ["bearer", "x-api-key", "query"])
def test_authentication_strategies_are_explicit(auth: str) -> None:
    server = MCPServer(sse=False)

    async def run() -> None:
        async with EUvsDisinfoAdapter.connect(
            EUvsDisinfoConnection(api_key="test-key", auth=auth, mode="stateless"),
            transport=httpx.MockTransport(server.handle),
        ) as source:
            await source.list_tools()

    asyncio.run(run())
    request = server.requests[0]
    assert request.url.host == "mcp.euvsd.info"
    assert request.url.path == "/mcp"
    if auth == "bearer":
        assert request.headers["Authorization"] == "Bearer test-key"
        assert "key" not in request.url.params
    elif auth == "x-api-key":
        assert request.headers["X-API-Key"] == "test-key"
        assert "Authorization" not in request.headers
        assert "key" not in request.url.params
    else:
        assert request.url.params["key"] == "test-key"
        assert "Authorization" not in request.headers and "X-API-Key" not in request.headers
    assert "test-key" not in repr(EUvsDisinfoConnection(api_key="test-key", auth=auth))


def test_all_four_tools_send_only_documented_arguments_and_keep_content() -> None:
    session = Mock(spec=ClientSession)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text='[{"title":"Source result"}]')]
    )
    source = EUvsDisinfoAdapter(session)

    async def run() -> None:
        articles = await source.articles(EUvsDisinfoPageRequest(offset=25, limit=25))
        assert articles.tool == "articles"
        await source.cases(EUvsDisinfoPageRequest(limit=100))
        search = await source.search(
            EUvsDisinfoSearchRequest(query="biolab", date_from="2024-01-01", date_to="2024-12-31")
        )
        assert search.content[0]["text"] == '[{"title":"Source result"}]'
        assert search.structured_content is None
        await source.narratives(EUvsDisinfoDateRange())
        await source.narratives(EUvsDisinfoDateRange(date_from="2025-01-01", date_to="2025-03-31"))

    asyncio.run(run())
    calls = session.call_tool.call_args_list
    assert [call.args[0] for call in calls] == ["articles", "cases", "search", "narratives", "narratives"]
    assert calls[0].kwargs["arguments"] == {"offset": 25, "limit": 25}
    assert calls[1].kwargs["arguments"] == {"offset": 0, "limit": 100}
    assert calls[2].kwargs["arguments"] == {
        "query": "biolab", "date_from": "2024-01-01", "date_to": "2024-12-31"
    }
    assert calls[3].kwargs["arguments"] == {}
    assert calls[4].kwargs["arguments"] == {"date_from": "2025-01-01", "date_to": "2025-03-31"}


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 1.5},
        {"offset": -1},
    ],
)
def test_pagination_rejects_invalid_values(options: dict) -> None:
    with pytest.raises(ValidationError):
        EUvsDisinfoPageRequest.model_validate(options)


@pytest.mark.parametrize(
    "options",
    [
        {"query": "ab"},
        {"query": "  a  "},
        {"query": "valid", "date_from": "2025-12-31", "date_to": "2025-01-01"},
        {"query": "valid", "date_from": "2025-02-30"},
    ],
)
def test_search_rejects_invalid_query_and_dates(options: dict) -> None:
    with pytest.raises(ValidationError):
        EUvsDisinfoSearchRequest.model_validate(options)


def test_tool_error_and_json_rpc_error_never_return_empty_success() -> None:
    session = Mock(spec=ClientSession)
    source = EUvsDisinfoAdapter(session)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="failed")], isError=True
    )
    with pytest.raises(EUvsDisinfoToolError):
        asyncio.run(source.cases(EUvsDisinfoPageRequest()))
    session.call_tool.side_effect = McpError(ErrorData(code=-32603, message="Internal error"))
    with pytest.raises(EUvsDisinfoRPCError) as raised:
        asyncio.run(source.search(EUvsDisinfoSearchRequest(query="Mariupol")))
    assert raised.value.code == -32603


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_decoded_result_size_limit_is_exact(extra_bytes: int) -> None:
    empty = CallToolResult(content=[TextContent(type="text", text="")])
    overhead = len(json.dumps(empty.model_dump(mode="json")).encode("utf-8"))
    result = CallToolResult(
        content=[TextContent(type="text", text="x" * (2_000_000 - overhead + extra_bytes))]
    )
    session = Mock(spec=ClientSession)
    session.call_tool.return_value = result
    source = EUvsDisinfoAdapter(session)
    if extra_bytes:
        with pytest.raises(RuntimeError, match="2 MB limit"):
            asyncio.run(source.cases(EUvsDisinfoPageRequest()))
    else:
        assert asyncio.run(source.cases(EUvsDisinfoPageRequest())).content[0]["type"] == "text"


def test_public_health_does_not_send_a_key() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://mcp.euvsd.info/health"
        assert "Authorization" not in request.headers
        assert "X-API-Key" not in request.headers
        return httpx.Response(
            200, json={"status": "ok", "timestamp": "2026-05-04T10:00:00Z", "authRequired": True}
        )

    health = asyncio.run(EUvsDisinfoAdapter.health(transport=httpx.MockTransport(handle)))
    assert health.auth_required
    assert health.timestamp == datetime(2026, 5, 4, 10, tzinfo=timezone.utc)
