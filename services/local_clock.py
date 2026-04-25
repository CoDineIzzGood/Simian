"""
Local-clock provider for Simian.

Pass S-C: the user reports the assistant inventing wrong times when
asked "what time is it" -- the LLM's notion of "now" defaults to its
training cutoff or to whatever the prompt context implies. This module
returns deterministic, system-clock-truth answers so the GUI can short-
circuit time/date questions BEFORE they reach the model.

Design:
  * Pure stdlib (datetime, locale-free formatting) so it can't fail at
    import time on a stripped Windows install.
  * Single source of truth for "now()" -- everything else in Simian
    that needs the local clock should call ``current_local()`` here so
    a future test harness can inject a fixed clock without touching
    every call site.
  * The string formatters return plain-English answers (12-hour AM/PM
    for time, full weekday + month name for date) since the user wants
    the assistant to read them aloud through TTS.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

# Phrases that should bypass the LLM entirely and answer from the local
# system clock. Tuned for both keyboard and voice input -- voice STT
# tends to drop punctuation, so we anchor on word boundaries rather
# than literal "?". Order doesn't matter; the first match wins.
TIME_QUERY_RE = re.compile(
    r"\b("
    r"what(?:'?s| is)\s+the\s+time"
    r"|what\s+time\s+is\s+it"
    r"|tell\s+me\s+the\s+time"
    r"|current\s+time"
    r"|do\s+you\s+(?:know|have)\s+the\s+time"
    r"|got\s+the\s+time"
    r"|the\s+time\s+please"
    r")\b",
    re.IGNORECASE,
)

DATE_QUERY_RE = re.compile(
    r"\b("
    r"what(?:'?s| is)\s+(?:the\s+)?(?:date|day)"
    r"|what\s+(?:date|day)\s+is\s+it"
    r"|today'?s\s+date"
    r"|tell\s+me\s+the\s+date"
    r"|current\s+date"
    r"|what\s+day\s+of\s+the\s+week"
    r")\b",
    re.IGNORECASE,
)

# Compound "what's the time and date" / "date and time" queries. We
# answer both in one reply rather than picking just one of the two.
DATE_AND_TIME_RE = re.compile(
    r"\b(?:date\s+and\s+time|time\s+and\s+date)\b",
    re.IGNORECASE,
)


def current_local() -> _dt.datetime:
    """Return the current local datetime.

    Single seam for tests: monkeypatch this to freeze time. Uses the
    naive local clock on purpose -- the user's report was that the
    assistant gave a wrong wall-clock time, which is what the OS
    clock shows in Windows' system tray.
    """
    return _dt.datetime.now()


def format_time(now: Optional[_dt.datetime] = None) -> str:
    """``It's 11:24 AM.`` style answer.

    Strips the leading zero on the hour (Windows' ``%I`` keeps it),
    keeps the minute zero-padded.
    """
    n = now or current_local()
    hour_12 = n.strftime("%I").lstrip("0") or "12"
    minute = n.strftime("%M")
    am_pm = n.strftime("%p")
    return f"It's {hour_12}:{minute} {am_pm}."


def format_date(now: Optional[_dt.datetime] = None) -> str:
    """``Today is Saturday, April 25, 2026.`` style answer."""
    n = now or current_local()
    weekday = n.strftime("%A")
    month = n.strftime("%B")
    # %d is zero-padded (04 vs 4); strip the leading zero for a more
    # natural read aloud.
    day = n.strftime("%d").lstrip("0") or "0"
    year = n.strftime("%Y")
    return f"Today is {weekday}, {month} {day}, {year}."


def format_date_and_time(now: Optional[_dt.datetime] = None) -> str:
    """Combined answer for "what's the date and time" style queries."""
    n = now or current_local()
    return f"{format_date(n)} {format_time(n)}"


def model_context_block(now: Optional[_dt.datetime] = None) -> str:
    """Return a short system-prompt block that grounds the model in the
    current local clock. Pass S-D feeds this into every LLM prompt so
    the model never invents a time/date when it leaks past the
    interceptor.

    The block is intentionally compact -- one line per fact -- so it
    eats minimal context window on small local models.
    """
    n = now or current_local()
    hour_12 = n.strftime("%I").lstrip("0") or "12"
    time_str = f"{hour_12}:{n.strftime('%M')} {n.strftime('%p')}"
    return (
        "Local clock context (truth, do not contradict):\n"
        f"- Today is {n.strftime('%A, %B %d, %Y')}.\n"
        f"- The current local time is {time_str}.\n"
        f"- ISO datetime: {n.strftime('%Y-%m-%dT%H:%M:%S')}.\n"
        "- The assistant's name is Simian.\n"
        "- If asked the current time or date, use the values above verbatim."
    )


def maybe_answer(text: str) -> Optional[str]:
    """Return a plain-English answer if ``text`` is a time/date query,
    otherwise None. Caller logs ``[Time] Answered from local system
    clock`` when this returns non-None.
    """
    if not text:
        return None
    if DATE_AND_TIME_RE.search(text):
        return format_date_and_time()
    if TIME_QUERY_RE.search(text):
        return format_time()
    if DATE_QUERY_RE.search(text):
        return format_date()
    return None
