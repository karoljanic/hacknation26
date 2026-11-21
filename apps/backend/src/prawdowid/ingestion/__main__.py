"""CLI: python -m prawdowid.ingestion {sync-catalog,ingest,embed,stats}"""

import argparse
import json
import logging
import sys
from dataclasses import asdict

from sqlalchemy import func, select

from prawdowid.domain.enums import SourceKind
from prawdowid.ingestion.catalog import load_catalog, sync_catalog
from prawdowid.ingestion.service import IngestionService
from prawdowid.persistence.db import session_factory
from prawdowid.persistence.models import Document, Passage, Source
from prawdowid.services.embedding import EmbedderUnavailable, default_embedder, embed_pending

# Corpus requirements from the task (sekcja 5: Źródła).
TARGET_DOCUMENTS, TARGET_DOMAINS, TARGET_KINDS = 100, 10, 2


def cmd_sync(_: argparse.Namespace) -> int:
    catalog = load_catalog()
    with session_factory()() as session, session.begin():
        print(json.dumps(sync_catalog(session, catalog)))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    with session_factory()() as session, session.begin():
        sync_catalog(session, catalog)
    service = IngestionService(session_factory=session_factory(), catalog=catalog)
    overrides = {"limit": args.limit} if args.limit is not None else None
    slugs = [e.slug for e in catalog.source] if args.all else args.source
    if args.collector:
        slugs = [e.slug for e in catalog.source if e.collect.collector == args.collector]
    if not slugs:
        print("Nothing to ingest: pass --source SLUG, --collector NAME or --all", file=sys.stderr)
        return 2
    worst = 0
    for slug in slugs:
        try:
            result = service.ingest_source(slug, overrides=overrides)
        except (KeyError, LookupError) as exc:
            print(f"{slug}: {exc}", file=sys.stderr)
            worst = 2
            continue
        line = {"source": slug, "status": result.status.value, **result.counts}
        print(json.dumps(line, ensure_ascii=False))
        for error in result.errors[: args.show_errors]:
            print(f"    {error}", file=sys.stderr)
        worst = max(worst, 1 if result.status.value in ("failed", "partial") else 0)
    return worst


def cmd_embed(args: argparse.Namespace) -> int:
    try:
        with default_embedder() as embedder:
            code = 0
            for target in args.target:
                result = embed_pending(
                    session_factory(),
                    embedder,
                    target=target,
                    limit=args.limit,
                    source_slug=args.source if target == "passage" else None,
                )
                print(json.dumps(asdict(result), ensure_ascii=False))
                code = code or (1 if result.errors else 0)
            return code
    except EmbedderUnavailable as exc:
        print(f"Embedding skipped: {exc}", file=sys.stderr)
        return 1


def cmd_stats(_: argparse.Namespace) -> int:
    with session_factory()() as session:
        rows = session.execute(
            select(
                Source.slug,
                Source.kind,
                Source.domain,
                Source.active,
                func.count(func.distinct(Document.id)),
                func.count(Passage.id),
                func.count(Passage.embedding_model),
                func.min(Document.published_at),
                func.max(Document.published_at),
            )
            .outerjoin(Document, Document.source_id == Source.id)
            .outerjoin(Passage, Passage.document_id == Document.id)
            .group_by(Source.id)
            .order_by(Source.kind, Source.slug)
        ).all()
    print(f"{'source':28} {'kind':13} {'domain':22} {'docs':>5} {'pass.':>6} {'emb.':>6}  published")
    docs = passages = embedded = 0
    domains: set[str] = set()
    kinds: set[SourceKind] = set()
    for slug, kind, domain, active, n_docs, n_pass, n_emb, oldest, newest in rows:
        span = f"{oldest:%Y-%m-%d}..{newest:%Y-%m-%d}" if oldest and newest else "-"
        flag = "" if active else " (inactive)"
        print(f"{slug + flag:28} {kind.value:13} {domain:22} {n_docs:5} {n_pass:6} {n_emb:6}  {span}")
        docs, passages, embedded = docs + n_docs, passages + n_pass, embedded + n_emb
        if n_docs:
            domains.add(domain)
            kinds.add(kind)
    print()
    print(f"documents: {docs}/{TARGET_DOCUMENTS}  domains: {len(domains)}/{TARGET_DOMAINS}  "
          f"source kinds: {len(kinds)}/{TARGET_KINDS}  passages: {passages} "
          f"(embedded {embedded})")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m prawdowid.ingestion", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync-catalog", help="Upsert data/sources/catalog.toml into the source table")

    ingest = sub.add_parser("ingest", help="Collect documents for catalogued sources")
    which = ingest.add_mutually_exclusive_group(required=True)
    which.add_argument("--source", nargs="+", metavar="SLUG")
    which.add_argument("--collector", metavar="NAME", help="All sources using this collector")
    which.add_argument("--all", action="store_true")
    ingest.add_argument("--limit", type=int, help="Override per-listing item limit (gov_pl)")
    ingest.add_argument("--show-errors", type=int, default=5, metavar="N")

    embed = sub.add_parser("embed", help="Embed passages/claims that have no vector yet")
    embed.add_argument("--target", nargs="+", choices=["passage", "claim"], default=["passage", "claim"])
    embed.add_argument("--limit", type=int, default=500)
    embed.add_argument("--source", metavar="SLUG", help="Only passages of this source")

    sub.add_parser("stats", help="Corpus size per source vs. the task's targets")

    args = parser.parse_args(argv)
    handlers = {"sync-catalog": cmd_sync, "ingest": cmd_ingest, "embed": cmd_embed, "stats": cmd_stats}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
