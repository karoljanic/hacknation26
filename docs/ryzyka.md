# Najważniejsze ryzyka

| Ryzyko | Ograniczanie i pozostałe zagrożenie |
| --- | --- |
| Halucynacje i błędne dopasowanie | System pokazuje źródła, cytaty i brak dowodów, ale model może dopasować podobne zdarzenie, błędnie zinterpretować datę lub wygenerować niepopartą odpowiedź. Wyniki wymagają kontroli człowieka, zwłaszcza w sprawach zdrowia i bezpieczeństwa. Podpis nie potwierdza prawdziwości. |
| Stronniczość i luki źródeł | Katalog preferuje źródła urzędowe i fact-checkingowe, ale nie zapewnia neutralności, kompletności ani niezależności źródeł. Brak lokalnych lub aktualnych danych może zniekształcić wynik; należy sprawdzać daty, perspektywy i sprzeczne dowody. |
| Prywatność | Redakcja rozpoznaje część danych osobowych, ale nie jest pełną anonimizacją. Tekst poddawany kontroli JEV może trafić do TypeSafe przed redakcją, a oryginalny obraz lub wideo do Google w celu odczytu. Metadane, linki i zredagowane treści mogą pozostawać w bazie; domyślny termin wygaśnięcia zgłoszenia to 30 dni, a usuwanie wymaga wykonania procedury retencji. Konieczne są przegląd podstaw prawnych, dostawców, dostępu i retencji. |
| Prompt injection | Pola tekstowe sprawdzają JEV i/lub lokalny Guardrails; HTML jest oczyszczany z części ukrytych treści. To nie gwarantuje wykrycia ataków, zwłaszcza w pobranych stronach lub mediach. Gdy lokalny detektor jest niedostępny, system może działać tylko z JEV; bez dostępnych detektorów zgłoszenie jest blokowane. |
| Dostępność | Awarie modeli i źródeł, limity i koszt usług oraz długie przetwarzanie mogą uniemożliwić odpowiedź. X API może zwracać 429; zaobserwowano też wiadomość niewidoczną przez API. Testy nie potwierdziły obsługi wszystkich awarii. |
| Nadużycia i koszty | Istnieją limity liczby i równoległości żądań, ale liczniki są lokalne dla procesu i zerują się po restarcie. To niewystarczająca ochrona dla wielu replik. Publiczne wdrożenie wymaga m.in. wspólnych limitów, monitorowania kosztów i ochrony przed automatycznym nadużyciem. |

Panel WWW administratora w obecnej wersji demonstracyjnej nie ma hasła
użytkownika: dostęp do strony daje możliwość korzystania z funkcji panelu,
jeśli skonfigurowano token po stronie serwera. Przed publicznym wdrożeniem
konieczne są rzeczywiste uwierzytelnienie i kontrola dostępu. Tokeny X
znajdują się na wolumenie bez szyfrowania na dysku i wymagają ochrony.
