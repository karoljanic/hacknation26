import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from prawdowid.adapters.sources.structured_data import PublicStructuredFetcher

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = REPO_ROOT / "data/sources/api/catalog.json"
ALLOWED_HOSTS = {
    "004": {"komunikaty.tvp.pl"},
    "007": {"www.policja.pl"},
    "008": {"mazowiecka.policja.gov.pl"},
    "013": {"api.gios.gov.pl"},
    "014": {"cert.pl"},
    "034": {"danepubliczne.imgw.pl"},
    "035": {"danepubliczne.imgw.pl"},
}
REQUEST_DELAY_SECONDS = 1.0


def _load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = raw.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"Invalid source catalog: {path}")
    result: dict[str, dict[str, object]] = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            raise ValueError(f"Invalid source record in {path}")
        source_id = source["id"]
        if source_id in result:
            raise ValueError(f"Duplicate source id in {path}: {source_id}")
        result[source_id] = source
    return result


def _endpoints(source: dict[str, object], source_id: str) -> list[str]:
    configured = source.get("entrypoints")
    if configured is None:
        configured = [source.get("entrypoint")] if source.get("entrypoint") else []
    if not isinstance(configured, list) or not configured:
        raise ValueError(f"Source {source_id} has no runnable endpoint in the catalog")
    if not all(isinstance(endpoint, str) for endpoint in configured):
        raise ValueError(f"Source {source_id} has an invalid endpoint list")
    hosts = ALLOWED_HOSTS.get(source_id)
    if hosts is None:
        raise ValueError(f"Source {source_id} is not enabled in the public API runner")
    for endpoint in configured:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.hostname not in hosts:
            raise ValueError(f"Source {source_id} endpoint is outside its HTTPS host allowlist")
    return configured


def _list_sources(catalog: dict[str, dict[str, object]]) -> None:
    for source_id, source in catalog.items():
        if source_id not in ALLOWED_HOSTS:
            continue
        endpoints = _endpoints(source, source_id)
        print(f"{source_id}  {source['title']}  ({len(endpoints)} endpoint(s))")


def _run_source(
    source_id: str,
    source: dict[str, object],
    fetcher: PublicStructuredFetcher,
) -> dict[str, object]:
    endpoints = _endpoints(source, source_id)
    responses = []
    last_host: str | None = None
    for endpoint in endpoints:
        host = urlsplit(endpoint).hostname
        if host == last_host:
            time.sleep(REQUEST_DELAY_SECONDS)
        response = fetcher.fetch(endpoint, allowed_hosts=ALLOWED_HOSTS[source_id])
        responses.append(
            {
                "source_url": source["url"],
                "endpoint": response.url,
                "retrieved_at": response.fetched_at.isoformat(),
                "http_status": response.status_code,
                "content_type": response.content_type,
                "data": response.data,
            }
        )
        last_host = host
    return {
        "source_id": source_id,
        "source_name": source["title"],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "responses": responses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch documented public APIs and feeds from the source catalog."
    )
    parser.add_argument("--list", action="store_true", help="List sources enabled in this runner")
    parser.add_argument("--source", help="Three-digit source ID from catalog.json")
    parser.add_argument("--output", type=Path, help="Write the JSON result to this path")
    args = parser.parse_args()
    if args.list == (args.source is not None):
        parser.error("Choose exactly one of --list or --source")

    catalog = _load_catalog()
    if args.list:
        _list_sources(catalog)
        return

    source = catalog.get(args.source)
    if source is None:
        parser.error(f"Unknown source ID: {args.source}")
    report = _run_source(args.source, source, PublicStructuredFetcher())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Result written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
