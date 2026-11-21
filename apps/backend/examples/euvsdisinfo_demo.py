import argparse
import asyncio
import os

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.domain.euvsdisinfo import (
    EUvsDisinfoConnection,
    EUvsDisinfoDateRange,
    EUvsDisinfoPageRequest,
    EUvsDisinfoSearchRequest,
)


async def run(args: argparse.Namespace) -> None:
    if args.command == "health":
        result = await EUvsDisinfoAdapter.health()
        print(result.model_dump_json(indent=2, by_alias=True))
        return

    if args.command in {"articles", "cases"}:
        request = EUvsDisinfoPageRequest(offset=args.offset, limit=args.limit)
    elif args.command == "search":
        request = EUvsDisinfoSearchRequest(
            query=args.query, date_from=args.date_from, date_to=args.date_to
        )
    elif args.command == "narratives":
        request = EUvsDisinfoDateRange(date_from=args.date_from, date_to=args.date_to)
    else:
        request = None

    connection = EUvsDisinfoConnection(
        api_key=os.environ["EUVSDISINFO_API_KEY"], mode=args.mode, auth=args.auth
    )
    async with EUvsDisinfoAdapter.connect(connection) as source:
        if args.command == "tools":
            result = await source.list_tools(cursor=args.cursor)
        elif isinstance(request, EUvsDisinfoPageRequest):
            result = (
                await source.articles(request)
                if args.command == "articles"
                else await source.cases(request)
            )
        elif isinstance(request, EUvsDisinfoSearchRequest):
            result = await source.search(request)
        elif isinstance(request, EUvsDisinfoDateRange):
            result = await source.narratives(request)
        else:
            raise ValueError("Unsupported EUvsDisinfo command")
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read EUvsDisinfo via Streamable HTTP MCP")
    parser.add_argument("--mode", choices=["stateful", "stateless"], default="stateful")
    parser.add_argument("--auth", choices=["bearer", "x-api-key", "query"], default="bearer")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Public health check; no API key required")
    tools = commands.add_parser("tools", help="List tool schemas, without calling remote tool names")
    tools.add_argument("--cursor")
    for name in ["cases", "articles"]:
        page = commands.add_parser(name)
        page.add_argument("--limit", type=int, default=10)
        page.add_argument("--offset", type=int, default=0)
    for name in ["search", "narratives"]:
        dated = commands.add_parser(name)
        if name == "search":
            dated.add_argument("--query", required=True)
        dated.add_argument("--date-from")
        dated.add_argument("--date-to")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
