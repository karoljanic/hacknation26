import argparse

from prawdowid.adapters.sources.gov_pl import GovPlHttpFetcher, GovPlParser

LISTING_URLS = [
    "https://www.gov.pl/web/rcb/raport-dobowy3",
    "https://www.gov.pl/web/kgpsp/aktualnosci",
    "https://www.gov.pl/web/obrona-narodowa/aktualnosci5",
    "https://www.gov.pl/web/gis/ostrzezenia",
    "https://www.gov.pl/web/paa/panstwowa-agencja-atomistyki",
    "https://www.gov.pl/web/infrastruktura/aktualnosci",
    "https://www.gov.pl/web/wody-polskie/komunikaty-nawigacyjne",
    "https://www.gov.pl/web/rcb/strona-glowna-rzadowego-centrum-bezpieczenstwa",
    "https://www.gov.pl/web/mswia",
    "https://www.gov.pl/web/premier",
]

STATIC_URLS = [
    "https://www.gov.pl/web/mswia/regionalny-system-ostrzegania",
    "https://www.gov.pl/web/infrastruktura/prawa-pasazerow",
    "https://www.gov.pl/web/infrastruktura/infolinia-dla-obywatela",
    "https://www.gov.pl/web/infrastruktura/dane-kontaktowe",
    "https://www.gov.pl/web/gddkia/informacja-drogowa---19-111",
]


def print_doc(doc) -> None:
    pub = doc.published_at.isoformat() if doc.published_at else "brak daty"
    preview = doc.text[:160].replace("\n", " ")
    print(f"  - [{doc.department}] {doc.title}")
    print(f"    URL: {doc.url}")
    print(f"    Data: {pub} | Znaki: {len(doc.text)} | PDF-y: {len(doc.attachments)}")
    print(f"    Fragment: {preview}...")
    if doc.errors:
        print(f"    UWAGI: {doc.errors}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Test parsera gov.pl na żywo")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--url")
    ap.add_argument("--static-only", action="store_true")
    args = ap.parse_args()

    with GovPlHttpFetcher() as fetcher:
        parser = GovPlParser(fetcher)

        if args.url:
            docs = parser.fetch_latest(args.url, limit=args.limit)
            print(f"Wynik dla {args.url}: {len(docs)} dok.")
            for doc in docs:
                print_doc(doc)
            return

        total = 0
        if not args.static_only:
            print("=== STRONY LISTUJĄCE ===")
            for url in LISTING_URLS:
                try:
                    docs = parser.fetch_latest(url, limit=args.limit)
                    total += len(docs)
                    print(f"\n[OK] {url} -> {len(docs)} dok.")
                    for doc in docs:
                        print_doc(doc)
                except Exception as exc:
                    print(f"\n[BŁĄD] {url} -> {exc}")

        print("\n=== STRONY STATYCZNE ===")
        for url in STATIC_URLS:
            try:
                doc = parser.fetch_page(url)
                if doc is None:
                    print(f"\n[PUSTE] {url}")
                else:
                    total += 1
                    print(f"\n[OK] {url}")
                    print_doc(doc)
            except Exception as exc:
                print(f"\n[BŁĄD] {url} -> {exc}")

        print(f"\nŁącznie pobrano: {total} dokumentów.")


if __name__ == "__main__":
    main()