import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from prawdowid.adapters.sources.site_crawler import (
    CRAWLER_USER_AGENT,
    DirectSourceCrawler,
    SourceCrawlResult,
)
from prawdowid.adapters.sources.web import PublicWebFetcher

REPO_ROOT = Path(__file__).resolve().parents[3]
BEGIN_MARKER = "<!-- BEGIN GENERATED DIRECT SUBPAGES -->"
END_MARKER = "<!-- END GENERATED DIRECT SUBPAGES -->"


def _read_urls(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]


def _inline(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _render_note_section(result: SourceCrawlResult, retrieved_at: str) -> str:
    lines = [
        BEGIN_MARKER,
        f"## Opis strony i bezpośrednie podstrony (crawl {retrieved_at[:10]})",
        "",
        f"- Wynik odczytu strony źródłowej: `{result.status}`.",
    ]
    if result.error:
        lines.append(f"- Błąd: {_inline(result.error)}.")
    if result.root:
        page = result.root
        lines.extend(
            [
                f"- Końcowy adres: <{page.final_url}>.",
                f"- Tytuł HTML: {_inline(page.title) if page.title else 'brak'}.",
                f"- Opis: {_inline(page.description)}",
            ]
        )
        if page.headings:
            lines.append(f"- Widoczne nagłówki: {'; '.join(map(_inline, page.headings))}.")
        if page.excerpt:
            lines.extend(["", f"> {_inline(page.excerpt)}"])
    lines.extend(["", f"### Podstrony linkowane bezpośrednio ({len(result.subpages)})", ""])
    if not result.subpages:
        lines.append("Nie znaleziono linków do podstron albo dostęp do strony był ograniczony.")
    for subpage in result.subpages:
        page = subpage.page
        heading = page.title if page and page.title else (
            subpage.link.labels[0] if subpage.link.labels else "Podstrona"
        )
        lines.extend(
            [
                f"#### {_inline(heading)}",
                f"- URL: <{subpage.link.url}>",
                f"- Tekst linku na stronie źródłowej: "
                f"{'; '.join(map(_inline, subpage.link.labels)) or 'brak'}",
                f"- Umiejscowienie linku: {', '.join(subpage.link.contexts) or 'nieustalone'}.",
                f"- Status pobrania: `{subpage.status}`.",
            ]
        )
        if subpage.error:
            lines.append(f"- Błąd: {_inline(subpage.error)}.")
        if page:
            lines.append(f"- Opis: {_inline(page.description)}")
            if page.headings:
                lines.append(f"- Nagłówki: {'; '.join(map(_inline, page.headings))}.")
            if page.excerpt:
                lines.extend(["", f"> {_inline(page.excerpt)}"])
        lines.append("")

    lines.extend([f"### Dokumenty i załączniki linkowane ({len(result.documents)})", ""])
    if result.documents:
        for document in result.documents:
            label = "; ".join(map(_inline, document.labels)) or "Załącznik"
            lines.append(f"- {label}: <{document.url}>")
    else:
        lines.append("Nie znaleziono bezpośrednio linkowanych plików.")
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def _update_note(path: Path, result: SourceCrawlResult, retrieved_at: str) -> None:
    content = path.read_text(encoding="utf-8")
    section = _render_note_section(result, retrieved_at)
    pattern = re.compile(
        re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL
    )
    if pattern.search(content):
        content = pattern.sub(lambda _: section, content)
    else:
        content = content.rstrip() + "\n\n" + section + "\n"
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch each URL in links.md and describe only same-host pages linked directly "
            "from that URL. Does not execute JavaScript or follow grandchildren."
        )
    )
    parser.add_argument("--links", type=Path, default=REPO_ROOT / "links.md")
    parser.add_argument("--notes-dir", type=Path, default=REPO_ROOT / "data/sources/links")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "data/sources/links/direct-crawl.json",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Minimum seconds between requests to the same origin; robots.txt delays also apply.",
    )
    parser.add_argument(
        "--update-notes",
        action="store_true",
        help="Replace the generated crawl section in each matching numbered note.",
    )
    args = parser.parse_args()

    urls = _read_urls(args.links)
    results = []
    crawler = DirectSourceCrawler(
        fetcher=PublicWebFetcher(
            user_agent=f"{CRAWLER_USER_AGENT}/0.1 (direct source inventory)"
        ),
        delay_seconds=args.delay,
    )
    note_paths = []
    if args.update_notes:
        for index, url in enumerate(urls, start=1):
            matching_notes = list(args.notes_dir.glob(f"{index:03d}-*.md"))
            if len(matching_notes) != 1:
                raise RuntimeError(
                    f"Expected one note for source {index:03d}, found {len(matching_notes)}"
                )
            note_path = matching_notes[0]
            note_url = re.search(
                r"^- URL: (\S+)", note_path.read_text(encoding="utf-8"), re.MULTILINE
            )
            if note_url is None or note_url.group(1) != url:
                raise RuntimeError(f"URL mismatch between {args.links} and {note_path}")
            note_paths.append(note_path)

    retrieved_at = datetime.now(timezone.utc).isoformat()
    for index, url in enumerate(urls, start=1):
        result = crawler.crawl(url)
        results.append(result)
        print(
            f"[{index}/{len(urls)}] {result.status}: {url} "
            f"({len(result.subpages)} direct pages)",
            file=sys.stderr,
        )

    report = {
        "retrieved_at": retrieved_at,
        "scope": "same-host direct links only; no recursive follow or JavaScript execution",
        "source_count": len(urls),
        "results": [asdict(result) for result in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.update_notes:
        for note_path, result in zip(note_paths, results, strict=True):
            _update_note(note_path, result, retrieved_at)
    print(f"JSON report written to {args.output}")


if __name__ == "__main__":
    main()
