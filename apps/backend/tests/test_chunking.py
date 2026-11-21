import pytest

from prawdowid.ingestion.chunking import chunk_text


def assert_spans_exact(text: str, chunks) -> None:
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(a.char_end <= b.char_start for a, b in zip(chunks, chunks[1:]))


def test_short_text_is_one_passage() -> None:
    text = "Pierwszy akapit.\nDrugi akapit."
    chunks = chunk_text(text)
    assert [(c.text, c.char_start, c.char_end) for c in chunks] == [(text, 0, len(text))]


def test_paragraphs_are_packed_and_never_split() -> None:
    paragraphs = [f"Akapit {i}. " + "słowo " * 40 for i in range(12)]  # ~250 chars each
    text = "\n".join(p.strip() for p in paragraphs)
    chunks = chunk_text(text, target_chars=600, max_chars=900)
    assert_spans_exact(text, chunks)
    assert len(chunks) > 1
    assert all(len(c.text) <= 900 for c in chunks)
    for chunk in chunks:  # every chunk starts at a paragraph start
        assert chunk.char_start == 0 or text[chunk.char_start - 1] == "\n"
    # Nothing lost: every non-whitespace character is covered.
    covered = "".join(c.text for c in chunks).replace("\n", "")
    assert covered == text.replace("\n", "")


def test_oversized_paragraph_splits_on_sentences() -> None:
    sentences = [f"To jest zdanie numer {i} z pewną treścią." for i in range(60)]
    text = " ".join(sentences)  # one paragraph, ~2600 chars
    chunks = chunk_text(text, target_chars=500, max_chars=800)
    assert_spans_exact(text, chunks)
    assert all(len(c.text) <= 800 for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)


def test_single_huge_sentence_is_hard_split_within_limit() -> None:
    text = "słowo " * 500
    text = text.strip()
    chunks = chunk_text(text, target_chars=300, max_chars=400)
    assert_spans_exact(text, chunks)
    assert all(0 < len(c.text) <= 400 for c in chunks)


def test_empty_and_whitespace_text() -> None:
    assert chunk_text("") == []
    assert chunk_text("\n \n") == []


def test_rejects_bad_limits() -> None:
    with pytest.raises(ValueError):
        chunk_text("x", target_chars=500, max_chars=100)
