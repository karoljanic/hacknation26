import argparse

from prawdowid.adapters.sources.imgw import ImgwHttpFetcher, ImgwParser
from prawdowid.adapters.sources.rso import RSO_ALL_XML_URL, RsoHttpFetcher, RsoParser
from prawdowid.adapters.sources.rss import RssHttpFetcher, RssParser

RSS_SOURCES = [
    ("CERT Polska", "https://moje.cert.pl/komunikaty/"),
    ("Policja.pl (RSS)", "https://policja.pl/dokumenty/rss/1-rss-1.rss"),
    ("KWP Mazowiecka", "https://mazowiecka.policja.gov.pl/ra/aktualnosci/aktualnosci"),
]


def print_doc(label: str, doc) -> None:
    pub = doc.published_at.isoformat() if doc.published_at else "brak daty"
    exp = getattr(doc, "expires_at", None)
    exp_str = f" | Wygasa: {exp.isoformat()}" if exp else ""
    preview = doc.text[:160].replace("\n", " ")
    print(f"  - [{label}] {doc.title}")
    print(f"    URL: {doc.url}")
    print(f"    Data: {pub}{exp_str} | Znaki: {len(doc.text)}")
    print(f"    Fragment: {preview}...")


def run_imgw(limit: int) -> int:
    print("=== IMGW-PIB (danepubliczne.imgw.pl) ===")
    total = 0
    with ImgwHttpFetcher() as fetcher:
        parser = ImgwParser(fetcher)
        datasets = [
            ("warningsmeteo", lambda: parser.fetch_warnings_meteo(limit=limit)),
            ("warningshydro", lambda: parser.fetch_warnings_hydro(limit=limit)),
            ("synop", lambda: parser.fetch_synop(limit=limit)),
            ("hydro", lambda: parser.fetch_hydro(limit=limit)),
        ]
        for name, fn in datasets:
            try:
                docs = fn()
                total += len(docs)
                print(f"\n[OK] IMGW {name} -> {len(docs)} dok.")
                for doc in docs:
                    print_doc(f"imgw:{name}", doc)
            except Exception as exc:
                print(f"\n[BŁĄD] IMGW {name} -> {exc}")
    return total


def run_rso(limit: int, province: str | None) -> int:
    print("\n=== RSO XML (komunikaty.tvp.pl) ===")
    with RsoHttpFetcher() as fetcher:
        parser = RsoParser(fetcher)
        try:
            docs = parser.fetch_warnings(url=RSO_ALL_XML_URL, province=province, limit=limit)
            label = f"rso:{province}" if province else "rso"
            print(f"\n[OK] {RSO_ALL_XML_URL} -> {len(docs)} dok.")
            for doc in docs:
                prov_str = ", ".join(doc.provinces) if doc.provinces else "cały kraj"
                print_doc(f"{label} | {prov_str}", doc)
            return len(docs)
        except Exception as exc:
            print(f"\n[BŁĄD] RSO -> {exc}")
            return 0


def run_rss(limit: int, custom_url: str | None) -> int:
    print("\n=== RSS / HTML (CERT, Policja) ===")
    sources = [("Custom", custom_url)] if custom_url else RSS_SOURCES
    total = 0
    with RssHttpFetcher() as fetcher:
        parser = RssParser(fetcher)
        for name, url in sources:
            try:
                docs = parser.fetch_source(url, limit=limit)
                total += len(docs)
                print(f"\n[OK] {name} ({url}) -> {len(docs)} dok.")
                for doc in docs:
                    print_doc(doc.domain, doc)
            except Exception as exc:
                print(f"\n[BŁĄD] {name} ({url}) -> {exc}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Test parserów IMGW, RSO i RSS na żywo")
    ap.add_argument("--only", choices=("imgw", "rso", "rss"), help="Przetestuj tylko jeden parser")
    ap.add_argument("--limit", type=int, default=3, help="Maks. dokumentów na źródło/dataset")
    ap.add_argument("--province", help="Filtr województwa dla RSO (np. mazowieckie)")
    ap.add_argument("--url", help="Przetestuj konkretny URL w parserze RSS")
    args = ap.parse_args()

    total = 0
    if args.only in (None, "imgw"):
        total += run_imgw(args.limit)
    if args.only in (None, "rso"):
        total += run_rso(args.limit, args.province)
    if args.only in (None, "rss"):
        total += run_rss(args.limit, args.url)

    print(f"\nŁącznie pobrano: {total} dokumentów.")


if __name__ == "__main__":
    main()