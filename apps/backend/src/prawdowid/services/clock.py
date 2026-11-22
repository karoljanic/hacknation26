"""The current date as the people asking see it (Polish time), for resolving
"dziś", "jutro" and "wczoraj" and for reasoning about the present."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

POLAND = ZoneInfo("Europe/Warsaw")


def today_in_poland() -> date:
    return datetime.now(POLAND).date()
