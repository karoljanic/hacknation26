import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import Implementation

from prawdowid.domain.euvsdisinfo import (
    EUvsDisinfoConnection,
    EUvsDisinfoDateRange,
    EUvsDisinfoHealth,
    EUvsDisinfoPageRequest,
    EUvsDisinfoResult,
    EUvsDisinfoSearchRequest,
    EUvsDisinfoTool,
    EUvsDisinfoToolList,
    EUvsDisinfoToolName,
)

MCP_URL = "https://mcp.euvsd.info/mcp"
HEALTH_URL = "https://mcp.euvsd.info/health"


class EUvsDisinfoRPCError(RuntimeError):
    def __init__(self, code: int) -> None:
        super().__init__(f"EUvsDisinfo MCP request failed with JSON-RPC code {code}")
        self.code = code


class EUvsDisinfoToolError(RuntimeError):
    pass


class EUvsDisinfoAdapter:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        connection: EUvsDisinfoConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> AsyncGenerator["EUvsDisinfoAdapter", None]:
        key = connection.api_key.get_secret_value()
        url = httpx.URL(MCP_URL)
        headers = {"Accept": "application/json, text/event-stream"}
        if connection.auth == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        elif connection.auth == "x-api-key":
            headers["X-API-Key"] = key
        else:
            url = url.copy_add_param("key", key)
        if connection.mode == "stateless":
            headers["mcp-session-id"] = str(uuid4())

        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(10, read=30),
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with streamable_http_client(
                str(url), http_client=client, terminate_on_close=connection.mode == "stateful"
            ) as (read, write, _):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                    client_info=Implementation(name="prawdowid", version="0.1.0"),
                ) as session:
                    if connection.mode == "stateful":
                        try:
                            await session.initialize()
                        except McpError as error:
                            raise EUvsDisinfoRPCError(error.error.code) from None
                    yield cls(session)

    @staticmethod
    async def health(
        *, transport: httpx.AsyncBaseTransport | None = None
    ) -> EUvsDisinfoHealth:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=transport
        ) as client:
            response = await client.get(HEALTH_URL)
            response.raise_for_status()
            return EUvsDisinfoHealth.model_validate_json(response.content)

    async def list_tools(self, *, cursor: str | None = None) -> EUvsDisinfoToolList:
        if cursor is not None and not cursor.strip():
            raise ValueError("Tool-list cursor must not be blank")
        try:
            result = await self.session.list_tools(cursor=cursor)
        except McpError as error:
            raise EUvsDisinfoRPCError(error.error.code) from None
        return EUvsDisinfoToolList(
            tools=[
                EUvsDisinfoTool(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.inputSchema,
                    output_schema=tool.outputSchema,
                )
                for tool in result.tools
            ],
            next_cursor=result.nextCursor,
        )

    async def cases(self, request: EUvsDisinfoPageRequest) -> EUvsDisinfoResult:
        return await self._call("cases", request.model_dump(mode="json"))

    async def articles(self, request: EUvsDisinfoPageRequest) -> EUvsDisinfoResult:
        return await self._call("articles", request.model_dump(mode="json"))

    async def search(self, request: EUvsDisinfoSearchRequest) -> EUvsDisinfoResult:
        return await self._call("search", request.model_dump(mode="json", exclude_none=True))

    async def narratives(self, request: EUvsDisinfoDateRange) -> EUvsDisinfoResult:
        return await self._call("narratives", request.model_dump(mode="json", exclude_none=True))

    async def _call(
        self, tool: EUvsDisinfoToolName, arguments: dict[str, object]
    ) -> EUvsDisinfoResult:
        try:
            result = await self.session.call_tool(
                tool, arguments=arguments, read_timeout_seconds=timedelta(seconds=30)
            )
        except McpError as error:
            raise EUvsDisinfoRPCError(error.error.code) from None
        if result.isError:
            raise EUvsDisinfoToolError(f"EUvsDisinfo tool {tool} reported an error")
        payload = result.model_dump(mode="json")
        if len(json.dumps(payload).encode("utf-8")) > 2_000_000:
            raise RuntimeError("EUvsDisinfo decoded tool result exceeds the 2 MB limit")
        return EUvsDisinfoResult(
            tool=tool,
            content=[item.model_dump(mode="json") for item in result.content],
            structured_content=result.structuredContent,
            fetched_at=datetime.now(timezone.utc),
        )
