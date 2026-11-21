from prawdowid.domain.claims import ClaimExtraction
from prawdowid.domain.fact_checks import (
    ClaimFactCheckMatch,
    ClaimFactCheckSearch,
    FactCheckPage,
    FactCheckSource,
    SearchOptions,
    TextSearchRequest,
)


class FactCheckTool:
    def __init__(self, source: FactCheckSource) -> None:
        self.source = source

    def search_claims(
        self,
        extraction: ClaimExtraction,
        *,
        language_code: str | None = "pl",
        page_size: int = 5,
        max_queries: int = 10,
    ) -> ClaimFactCheckSearch:
        if max_queries < 1:
            raise ValueError("max_queries must be positive")
        if len({claim.id for claim in extraction.claims}) != len(extraction.claims):
            raise ValueError("Claim IDs must be unique")
        options = SearchOptions(language_code=language_code, page_size=page_size)
        candidates = [claim for claim in extraction.claims if claim.kind != "opinion"]
        queries = {
            claim.text: TextSearchRequest(
                query=claim.text,
                language_code=options.language_code,
                page_size=options.page_size,
            )
            for claim in candidates
        }
        if len(queries) > max_queries:
            raise ValueError("Claim search exceeds the query limit; split the input into batches")
        pages: dict[str, FactCheckPage] = {
            query: self.source.search_text(request) for query, request in queries.items()
        }
        return ClaimFactCheckSearch(
            matches=[
                ClaimFactCheckMatch(claim_id=claim.id, query=claim.text, page=pages[claim.text])
                for claim in candidates
            ],
            skipped_opinion_ids=[claim.id for claim in extraction.claims if claim.kind == "opinion"],
        )
