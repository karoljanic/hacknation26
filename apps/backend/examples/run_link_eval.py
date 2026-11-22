"""Runs the link fact set (data/eval/link_facts.json) through a live POST /check.

Every case is a true fact taken from an official page listed in links.md, so
the expected verdict is ``confirmed``. ``insufficient_evidence`` is a safe
abstention (a missing source, not a wrong answer); ``false`` or ``misleading``
is a wrong answer.

    python examples/run_link_eval.py --api http://127.0.0.1:8001 --out /tmp/link_eval.json
"""

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_CASES = Path(__file__).resolve().parents[3] / "data" / "eval" / "link_facts.json"


def run_case(client: httpx.Client, api: str, case: dict) -> dict:
    started = time.monotonic()
    for _ in range(5):
        response = client.post(f"{api}/check", data={"type": "text", "text": case["claim"]})
        if response.status_code not in (429, 503):
            break
        time.sleep(float(response.headers.get("retry-after", "10")))  # rate limit: wait and retry
    elapsed = time.monotonic() - started
    try:
        body = response.json()
    except ValueError:
        body = {"status": f"http_{response.status_code}", "claims": []}
    claims = body.get("claims") or []
    verdicts = [claim["verdict"] for claim in claims]
    if body.get("status") != "ok":
        outcome = "error"
    elif any(verdict in case["expected"] for verdict in verdicts):
        outcome = "pass"
    elif any(verdict in ("false", "misleading") for verdict in verdicts):
        outcome = "wrong"
    else:
        outcome = "abstain"
    return {
        "id": case["id"],
        "url": case["url"],
        "claim": case["claim"],
        "status": body.get("status"),
        "verdicts": verdicts,
        "bases": [claim.get("basis") for claim in claims],
        "sources": sorted({urlsplit(s["url"]).netloc for claim in claims for s in claim.get("sources", [])}),
        "explanation": claims[0]["explanation"] if claims else body.get("message"),
        "outcome": outcome,
        "seconds": round(elapsed, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=Path("link_eval_results.json"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--only", nargs="*", help="Case IDs to run (default: all)")
    args = parser.parse_args()

    cases = [case for case in json.loads(args.cases.read_text())["cases"] if "id" in case]
    if args.only:
        cases = [case for case in cases if case["id"] in set(args.only)]
    with httpx.Client(timeout=180) as client, ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda case: run_case(client, args.api, case), cases))
    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))

    by_url: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        by_url[result["url"]].append(result)
    for url, items in by_url.items():
        marks = " ".join(f"{r['id']}:{r['outcome']}({','.join(r['verdicts']) or r['status']})" for r in items)
        print(f"{url[:70]:70} {marks}")
    totals = Counter(result["outcome"] for result in results)
    seconds = sorted(result["seconds"] for result in results)
    print(f"\n{dict(totals)} of {len(results)}; median {seconds[len(seconds) // 2]}s, max {seconds[-1]}s")


if __name__ == "__main__":
    main()
