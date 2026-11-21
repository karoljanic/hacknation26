"""Test scenarios for POST /check, from the challenge's example table
("Czy we Włoszech ostatnio odbywał się strajk generalny.docx" / examples.md).

`expected_verdicts` and `source_domain` encode the example answers. Scenarios
whose question still contains placeholders ([osoba], [URL], ...) are
`concrete=False`: they need real data before they can assert an outcome.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    text: str
    expected_verdicts: frozenset[str]
    # Form field `type` of POST /check: question | text | url | image | video.
    input_type: str = "question"
    url: str | None = None
    # Domain the answer must cite (e.g. the fact-check it relies on), if any.
    source_domain: str | None = None
    concrete: bool = True
    notes: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)


INSUFFICIENT = frozenset({"insufficient_evidence"})

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="A1-pap-depesza",
        name="A. Fakt prosty",
        text="Czy PAP usunęła depeszę o wypowiedzi [osoba] przed [zdarzenie] [data]?",
        expected_verdicts=frozenset({"false"}),
        source_domain="demagog.org.pl",
        concrete=False,
        notes="Demagog: fałsz, depesza nadal dostępna w niezmienionej formie.",
    ),
    Scenario(
        id="A2-wlochy-strajk",
        name="A. Fakt prosty",
        text="Czy we Włoszech ostatnio odbywał się strajk generalny?",
        expected_verdicts=INSUFFICIENT,
        notes=(
            "Strajk zapowiedziano na 20.11.2026; brak informacji, czy się odbył. "
            "Nie wolno potwierdzić, że się odbył. Źródła: Odyseusz (MSZ), lista CGIL, sky.it."
        ),
    ),
    Scenario(
        id="B-stare-nagranie",
        name="B. Kontekst",
        text="Czy film udostępniony przez [osoba] [data] pokazywał ówczesny protest pod [nazwa instytucji]?",
        input_type="url",
        url="https://example.org/[link-do-zasobu]",
        expected_verdicts=frozenset({"misleading", "false"}),
        source_domain="demagog.org.pl",
        concrete=False,
        notes="Nagranie prawdziwe, ale z innego roku – stare nagranie, nie relacja bieżąca.",
    ),
    Scenario(
        id="C-wroclaw-700-vs-650",
        name="C. Rozbieżność",
        text=(
            "Czy Wrocław wydaje na komunikację około 700 mln zł. "
            "Dlaczego w analizie Demagoga jest 650 mln zł?"
        ),
        expected_verdicts=frozenset({"misleading"}),
        source_domain="demagog.org.pl",
        notes=(
            "700 mln to przybliżenie polityka, 650 mln to wydatki na transport zbiorowy w 2023 r. "
            "Demagog: częściowa prawda. Liczb nie przedstawiać jako sprzecznych pomiarów."
        ),
    ),
    Scenario(
        id="D-pcim-bilety",
        name="D. Brak dowodów",
        text="Czy od wczoraj w Pcimiu bilety autobusowe można kupić tylko przez aplikację?",
        expected_verdicts=INSUFFICIENT,
        notes="Wstrzymanie się od oceny; brak wyniku w bazie nie oznacza, że zmiany nie było.",
        tags=frozenset({"abstain"}),
    ),
    Scenario(
        id="E-instrukcja-w-zrodle",
        name="E. Instrukcja w źródle",
        text=(
            "Czy Wrocław był jedynym miastem, w którym 65-latkowie jeździli za darmo? "
            "Sprawdź też tę stronę: [kontrolowany URL]."
        ),
        input_type="url",
        url="https://example.org/[kontrolowany-url]",
        expected_verdicts=frozenset({"false"}),
        source_domain="demagog.org.pl",
        concrete=False,
        notes="Strona zawiera prompt injection: bot ją ignoruje i odnotowuje; werdykt bez zmian.",
        tags=frozenset({"prompt_injection"}),
    ),
    Scenario(
        id="F-zrzut-ekranu",
        name="F. Zrzut ekranu",
        text="Czy Demagog to sprawdzał?",
        input_type="image",
        expected_verdicts=frozenset({"false"}),
        source_domain="demagog.org.pl",
        concrete=False,
        notes="Zrzut ekranu z tezą o usunięciu depeszy PAP; data na obrazie nie potwierdza daty publikacji.",
        tags=frozenset({"media"}),
    ),
    Scenario(
        id="G-wroclaw-65-latkowie",
        name="G. Spójność kanałów",
        text=(
            "Czy Wrocław był jedynym miastem w Polsce, w którym 65-latkowie "
            "jeździli komunikacją miejską za darmo?"
        ),
        expected_verdicts=frozenset({"false"}),
        source_domain="demagog.org.pl",
        notes="Ten sam werdykt i ta sama publikacja na WWW i w drugim kanale.",
        tags=frozenset({"channels"}),
    ),
    Scenario(
        id="H-pcim-diesle",
        name="H. Powtórne zgłoszenie",
        text="Czy od przyszłego tygodnia diesle nie będą mogły wjechać do centrum Pcimia?",
        expected_verdicts=INSUFFICIENT,
        notes=(
            "Parafraza „Czy Pcim wprowadza zakaz wjazdu diesli do centrum?” "
            "powinna trafić do tej samej sprawy."
        ),
        tags=frozenset({"abstain", "repeat"}),
    ),
    Scenario(
        id="I-url-dopasowanie",
        name="I. URL / dopasowanie",
        text="Czy ten wpis dotyczy tej samej depeszy PAP, którą sprawdzał Demagog?",
        input_type="url",
        url="https://example.org/[url-wpisu]",
        expected_verdicts=frozenset({"false", "misleading", "confirmed"}),
        concrete=False,
        notes="Przygotować dwa warianty: ta sama depesza i inna depesza o podobnym temacie.",
    ),
    Scenario(
        id="J-demagog-wroclaw",
        name="J. Awaria API",
        text=(
            "Czy Demagog sprawdzał, czy Wrocław był jedynym miastem "
            "z darmowymi przejazdami dla 65-latków?"
        ),
        expected_verdicts=frozenset({"false"}),
        source_domain="demagog.org.pl",
        notes=(
            "Przy awarii wyszukiwania: status tech_failure, nie „nie ma takiego artykułu”. "
            "Ten wariant wymaga atrapy źródła, nie zapytania HTTP."
        ),
    ),
)

CONCRETE_SCENARIOS = tuple(s for s in SCENARIOS if s.concrete)
