import pytest

from prawdowid.domain.quotes import locate_quote, source_span

SOURCE = "Wrocław nie jest jedynym miastem. Z\xa0takiej ulgi korzystają seniorzy i\xa0seniorki w\xa0Toruniu.\n\nDalej."


def test_exact_quote_is_found() -> None:
    assert source_span(SOURCE, "nie jest jedynym miastem.") == "nie jest jedynym miastem."


def test_non_breaking_spaces_and_line_breaks_are_tolerated() -> None:
    quote = "Z takiej ulgi korzystają seniorzy i seniorki w Toruniu. Dalej."
    span = source_span(SOURCE, quote)

    # The returned span is the source's own text, not the model's rendering.
    assert span == "Z\xa0takiej ulgi korzystają seniorzy i\xa0seniorki w\xa0Toruniu.\n\nDalej."
    start, end = locate_quote(SOURCE, quote)
    assert SOURCE[start:end] == span


def test_typographic_quotes_and_dashes_are_tolerated() -> None:
    source = "Minister powiedział: „to nieprawda” – dodał."
    assert source_span(source, 'Minister powiedział: "to nieprawda" - dodał.') == source


@pytest.mark.parametrize(
    "quote",
    [
        "Wrocław jest jedynym miastem.",  # paraphrase / changed meaning
        "wrocław nie jest jedynym miastem.",  # case is not folded
        "   ",
    ],
)
def test_anything_beyond_presentation_differences_is_rejected(quote: str) -> None:
    assert locate_quote(SOURCE, quote) is None


def test_quote_shortened_with_an_elision_maps_to_the_full_contiguous_span() -> None:
    text = "Droga wodna obejmuje rzekę Wisłę od ujścia Przemszy do Kanału Łączańskiego i dalej do ujścia Sanny."

    span = source_span(text, "Droga wodna obejmuje rzekę Wisłę [...] do ujścia Sanny.")

    assert span == text


def test_elided_parts_out_of_order_or_too_far_apart_do_not_match() -> None:
    text = "Pierwsze zdanie. " + "Wypełniacz. " * 60 + "Ostatnie zdanie."

    assert source_span(text, "Ostatnie zdanie. … Pierwsze zdanie.") is None
    assert source_span(text, "Pierwsze zdanie. … Ostatnie zdanie.") is None
