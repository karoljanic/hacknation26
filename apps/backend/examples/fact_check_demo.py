import argparse
import os

import httpx

from prawdowid.adapters.sources.google_fact_check import GoogleFactCheckAdapter
from prawdowid.domain.fact_checks import ImageSearchRequest, TextSearchRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Search existing fact-checks, not truth verdicts")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--query")
    inputs.add_argument("--image-url", help="Public HTTP(S) image URL; no file upload")
    parser.add_argument("--publisher")
    parser.add_argument("--language")
    parser.add_argument("--max-age-days", type=int)
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--page-token")
    parser.add_argument("--offset", type=int)
    args = parser.parse_args()
    options = {
        "language_code": args.language,
        "page_size": args.page_size,
        "page_token": args.page_token,
        "offset": args.offset,
    }
    request: ImageSearchRequest | TextSearchRequest
    if args.image_url is not None:
        if args.publisher is not None or args.max_age_days is not None:
            parser.error("Publisher and max-age filters are only supported for text search")
        request = ImageSearchRequest(image_url=args.image_url, **options)
    else:
        request = TextSearchRequest(
            query=args.query,
            publisher_site=args.publisher,
            max_age_days=args.max_age_days,
            **options,
        )
    with httpx.Client() as client:
        source = GoogleFactCheckAdapter(client=client, api_key=os.environ["FACTCHECK_API_KEY"])
        if isinstance(request, ImageSearchRequest):
            result = source.search_image(request)
        else:
            result = source.search_text(request)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
