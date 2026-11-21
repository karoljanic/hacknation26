"""CLI: python -m prawdowid.ingestion {sync-catalog,ingest,embed,stats,jobs}

Commands that change data (sync-catalog, ingest, embed) are recorded in the
``job_run`` table like worker tasks; ``jobs`` shows that log.
"""

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from prawdowid.domain.enums import IngestionStatus, JobStatus, SourceKind
from prawdowid.ingestion.catalog import load_catalog, sync_catalog
from prawdowid.ingestion.service import IngestionService
from prawdowid.persistence.db import session_factory
from prawdowid.persistence.job_log import track_job
from prawdowid.persistence.models import Document, JobRun, Passage, Source
from prawdowid.services.embedding import EmbedderUnavailable, default_embedder, embed_pending

# Corpus requirements from the task (sekcja 5: Źródła).
TARGET_DOCUMENTS, TARGET_DOMAINS, TARGET_KINDS = 100, 10, 2


def cmd_sync(_: argparse.Namespace) -> int:
    with track_job("cli.sync-catalog", trigger="cli") as job:
        catalog = load_catalog()
        with session_factory()() as session, session.begin():
            stats = sync_catalog(session, catalog)
        job.record(stats)
        print(json.dumps(stats))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    params = {"source": args.source, "collector": args.collector, "all": args.all, "limit": args.limit}
    with track_job("cli.ingest", trigger="cli", params=params) as job:
        catalog = load_catalog()
        with session_factory()() as session, session.begin():
            sync = sync_catalog(session, catalog)
        service = IngestionService(session_factory=session_factory(), catalog=catalog)
        overrides = {"limit": args.limit} if args.limit is not None else None
        slugs = [e.slug for e in catalog.source] if args.all else args.source
        if args.collector:
            slugs = [e.slug for e in catalog.source if e.collect.collector == args.collector]
        if not slugs:
            print("Nothing to ingest: pass --source SLUG, --collector NAME or --all", file=sys.stderr)
            job.record({"catalog_sync": sync, "sources": {}}, problems=["no sources selected"])
            return 2
        worst, per_source, problems = 0, {}, []
        for slug in slugs:
            try:
                result = service.ingest_source(slug, overrides=overrides, job_id=job.id)
            except (KeyError, LookupError) as exc:
                print(f"{slug}: {exc}", file=sys.stderr)
                problems.append(f"{slug}: {exc}")
                worst = 2
                continue
            per_source[slug] = {"status": result.status.value, "ingestion_run_id": str(result.run_id),
                                **result.counts}
            if result.status is not IngestionStatus.OK:
                problems.extend(f"{slug}: {e}" for e in result.errors)
            print(json.dumps({"source": slug, "status": result.status.value, **result.counts},
                             ensure_ascii=False))
            for error in result.errors[: args.show_errors]:
                print(f"    {error}", file=sys.stderr)
            worst = max(worst, 1 if result.status.value in ("failed", "partial") else 0)
        totals = {key: sum(r.get(key, 0) for r in per_source.values())
                  for key in ("seen", "new", "unchanged", "skipped", "failed", "passages")}
        job.record({"catalog_sync": sync, "totals": totals, "sources": per_source},
                   problems=problems, ok=not problems)
    return worst


def cmd_embed(args: argparse.Namespace) -> int:
    params = {"target": args.target, "limit": args.limit, "source": args.source}
    with track_job("cli.embed", trigger="cli", params=params) as job:
        try:
            with default_embedder() as embedder:
                results = [
                    asdict(embed_pending(session_factory(), embedder, target=target, limit=args.limit,
                                         source_slug=args.source if target == "passage" else None))
                    for target in args.target
                ]
        except EmbedderUnavailable as exc:
            print(f"Embedding skipped: {exc}", file=sys.stderr)
            job.record({"targets": {}}, problems=[str(exc)])
            return 1
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
        problems = [f"{r['target']}: {e}" for r in results for e in r["errors"]]
        job.record({"targets": {r["target"]: r for r in results}}, problems=problems)
        return 1 if problems else 0


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


def cmd_jobs(args: argparse.Namespace) -> int:
    query = select(JobRun).order_by(JobRun.started_at.desc()).limit(args.limit)
    if args.problems:
        query = query.where(JobRun.status.in_([JobStatus.PROBLEMS, JobStatus.FAILED]))
    if args.name:
        query = query.where(JobRun.name.like(f"{args.name}%"))
    if args.since_hours:
        query = query.where(JobRun.started_at >= datetime.now(timezone.utc) - timedelta(hours=args.since_hours))
    with session_factory()() as session:
        jobs = session.scalars(query).all()
    for job in jobs:
        took = f"{job.duration_ms / 1000:.1f}s" if job.duration_ms is not None else "running"
        print(f"{job.started_at:%Y-%m-%d %H:%M:%S}  {job.status.value:8} {took:>8}  {job.name:26} "
              f"{job.trigger:6} {job.id}")
        summary = job.result.get("totals") or {k: v for k, v in job.result.items() if not isinstance(v, dict | list)}
        if summary:
            print(f"    {json.dumps(summary, ensure_ascii=False, default=str)[:200]}")
        if job.error:
            print(f"    error: {job.error}")
        for problem in job.problems[: args.show_problems]:
            print(f"    ! {problem}")
        if len(job.problems) > args.show_problems:
            print(f"    … {len(job.problems) - args.show_problems} more problems")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m prawdowid.ingestion", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
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

    jobs = sub.add_parser("jobs", help="Recent worker/CLI jobs from the job_run log")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--problems", action="store_true", help="Only jobs with problems or failures")
    jobs.add_argument("--name", help="Job name prefix, e.g. 'ingestion.' or 'cli.embed'")
    jobs.add_argument("--since-hours", type=float)
    jobs.add_argument("--show-problems", type=int, default=3, metavar="N")

    args = parser.parse_args(argv)
    handlers = {"sync-catalog": cmd_sync, "ingest": cmd_ingest, "embed": cmd_embed, "stats": cmd_stats,
                "jobs": cmd_jobs}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
