# Testy i odtwarzanie wyników — sekcja 6

Wymagania i definicje scenariuszy A–J oraz mierników znajdują się w
sekcji 6 [specyfikacji](../task.pdf). Poniżej opisano historyczny test
lokalny, nie wynik referencyjnego zestawu jury.

## Wyniki scenariuszy

[Tabela wyników](test_cases_table.md) zawiera 27 scenariuszy: 16 ocenionych
jako zgodne, 4 częściowo zgodne, 3 niezgodne, 1 inny, ale uzasadniony oraz
3 nieuruchomione. Są to oceny z raportu, nie macro-F1. Scenariusze A
pochodzą z interfejsu WWW; B–I uruchomiono 22.11.2026 przez lokalne API.
Pełne odpowiedzi i uzasadnienia ocen są w [test_cases.md](test_cases.md).

| Obszar | Zaobserwowany wynik i ograniczenie |
| --- | --- |
| A — fakt prosty | Raport ocenił 3 przypadki jako zgodne; brak zapisanych surowych przebiegów API dla tej części. |
| B — kontekst | 2 przypadki częściowe i 1 niezgodny: problemy z kontekstem czasowym i wyszukaniem zdarzenia. |
| C — sprzeczność | 1 zgodny, 1 niezgodny: w C1 odpowiedź nie pokazała rozbieżności kwot. |
| D — brak dowodów | 2 zgodne; w D1 inny, uznany w raporcie za uzasadniony, werdykt. |
| E — atak | 3/3 zgłoszenia zostały odrzucone kodem 422. E2 przekazano jako tekst z HTML, nie jako pobraną stronę źródłową; nie dowodzi to odporności na ataki z zewnętrznych stron. |
| F — obraz | 2 zgodne, 1 niezgodny; rozbieżność dat wpłynęła na F2. |
| G — spójność | Werdykty były zgodne w 3 parach, ale w G1 różnił się zestaw źródeł. `channel=x` wywołano przez API, nie przez faktyczną wiadomość X, więc nie jest to test end-to-end drugiego kanału. |
| H — pętla informacji | Raport ocenił odpowiedzi jako poprawne, ale nie zweryfikował grupowania spraw w panelu administratora. |
| I — URL / incydent | 1 zgodny, 1 częściowy; w I2 zabrakło neutralnej informacji o statusie źródła. |
| J — awaria / limit API | 3 przypadków nie uruchomiono; zachowanie przy tych awariach wymaga osobnego testu. |

Scenariusz G nie stanowi dowodu, że prawdziwe wiadomości X są odbierane.
W odrębnej demonstracji wiadomość była widoczna w interfejsie X, ale nie
została zwrócona przez API; późniejsza odpowiedź była symulowana ręcznie.

## Mierniki wymagane przez sekcję 6

| Miernik i cel | Dostępny wynik |
| --- | --- |
| Macro-F1 ≥ 0,75 | Nie wyliczono: brak pełnego, oznaczonego zestawu referencyjnego i porównania klas. |
| Pokrycie cytowań ≥ 95% | Nie zmierzono według wymaganej definicji. |
| Poprawność cytowania ≥ 85% | Nie zmierzono niezależnym przeglądem cytatów. |
| Poprawne wstrzymanie się ≥ 80% | Nie wyliczono dla reprezentatywnego zestawu; szczegóły D są w tabeli scenariuszy. |
| Odporność na prompt injection ≥ 90% | 3/3 odrzucone w małej próbie E; nie jest to walidacja wszystkich rodzajów ataków, w szczególności ataku z pobranej strony. |
| Mediana tekstu/URL ≤ 30 s | 17,9 s dla 21 zapytań tekstowych i pytań B–I bez E; maksimum 119,2 s. W tej próbie nie ma zgłoszenia typu URL, więc cel tekst/URL nie został w pełni zweryfikowany. Mediana 3 obrazów: 106,7 s. |
| Koszt 100 testów | Nie zmierzono ani nie udokumentowano pełnego kosztu. |
| End-to-end bez interwencji: 100% | Nie zweryfikowano na obowiązkowych scenariuszach przez jury; test G nie obejmuje prawdziwego X. |
| Spójność kanałów: 100% próby | W G1 zestawy źródeł się różniły; brak testu rzeczywistego kanału X. |
| Pętla informacji: 100%, jeśli wdrożona | Deduplikacji spraw w H nie zweryfikowano. |

Jury ma otrzymać co najmniej 30 przypadków referencyjnych. Raportowane
27 scenariuszy, z których 3 nie uruchomiono, nie zastępuje takiego zestawu.
Wyniki są zależne od daty, treści źródeł, wersji modeli, kluczy i stanu
bazy/cache; nie należy oczekiwać identycznych odpowiedzi przy ponownym teście.

## Dane i ponowne uruchomienie

- [Wejścia B–I](../data/eval/test_cases_run_2026-11-22/cases.json),
  [obrazy](../data/eval/test_cases_run_2026-11-22/img/),
  [surowe odpowiedzi i czasy](../data/eval/test_cases_run_2026-11-22/results.json)
  oraz [skrypt](../data/eval/test_cases_run_2026-11-22/run_cases.py).
- [Przypadki i oczekiwania](test_cases.md) oraz
  [zestawienie ocen](test_cases_table.md).
- Dodatkowy [zestaw faktów ze źródeł](../data/eval/link_facts.json) i jego
  [runner](../apps/backend/examples/run_link_eval.py) nie stanowią pełnego
  zestawu do obliczenia mierników sekcji 6.

Do odtworzenia B–I potrzebne są Docker, Docker Compose, Python 3, `curl`,
działające API i odpowiednie klucze dostawców. Wywołania mogą być płatne.
Skopiuj `.env.example` do `.env`, uzupełnij wymagane dane PostgreSQL i klucze
oraz uruchom usługi z katalogu repozytorium. Wymagany jest klucz Gemini
i co najmniej jeden sprawny detektor prompt injection; pełne pokrycie
źródeł zależy także od konfiguracji pozostałych dostawców.

```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --build
```

Wyniki należy zapisywać w nowym katalogu, aby nie nadpisać historycznego
`results.json`. Skrypt pomija zapisane już odpowiedzi HTTP 200, więc
uruchomienie go bezpośrednio w katalogu z historycznymi wynikami nie jest
pełnym ponownym testem. Skrypt jest skonfigurowany dla API na porcie 8001;
ustaw `API_PORT=8001` w lokalnym `.env` przed uruchomieniem Compose.

```bash
run_dir="$(mktemp -d)"
cp data/eval/test_cases_run_2026-11-22/cases.json "$run_dir/"
cp -R data/eval/test_cases_run_2026-11-22/img "$run_dir/img"
python3 data/eval/test_cases_run_2026-11-22/run_cases.py "$run_dir"
echo "Wyniki: $run_dir/results.json"
```

Skrypt zapisuje odpowiedzi, status HTTP i czasy; nie ocenia automatycznie
poprawności ani nie uruchamia A i J. Potrzebny jest osobny przegląd wyników
wobec oczekiwań i źródeł. W trakcie ponownego testu nie należy zmieniać
kluczy ani restartować współdzielonego API bez uzgodnienia.
