from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Iterable


EXTRACTION_SYSTEM_PROMPT = """
Extract a payment promise from the customer's message.

Evidence contract:
- Every item in evidence_quotes MUST be copied character-for-character from the
  original customer message.
- Evidence must be a contiguous substring. Do not normalize currency, spelling,
  capitalization, whitespace, dates, or numbers inside a quote.
- For example, if the message contains "40k", quote "40k"; never quote
  "₹40,000".
- If the required fact has no exact supporting span, return needs_review=true.

Date contract:
- Return the customer's original date phrase in promised_date_text.
- Copy the smallest useful verbatim span, such as "by Friday" or
  "next Friday"; do not paraphrase it as a calendar date.
- Do not independently convert relative weekday phrases into calendar dates.
- The backend resolves them using the documented deterministic rule.
""".strip()


WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class EvidenceSpan:
    quote: str
    start: int
    end: int


class ContractError(ValueError):
    """Raised when an extraction violates a deterministic contract."""


def locate_exact_evidence(message: str, quotes: Iterable[str]) -> list[EvidenceSpan]:
    """Locate exact, non-empty evidence quotes in the unmodified input.

    This intentionally performs no fuzzy matching or normalization. A quote that
    paraphrases the source is invalid, even if the paraphrase is semantically
    correct, because it cannot be presented as verbatim audit evidence.
    """
    located: list[EvidenceSpan] = []
    for quote in quotes:
        if not quote:
            raise ContractError("Evidence quotes must not be empty")
        start = message.find(quote)
        if start < 0:
            raise ContractError(f"Evidence is not an exact input substring: {quote!r}")
        located.append(EvidenceSpan(quote=quote, start=start, end=start + len(quote)))
    if not located:
        raise ContractError("At least one evidence quote is required")
    return located


def resolve_relative_weekday(phrase: str, message_timestamp: datetime) -> datetime:
    """Resolve a deliberately small set of English weekday expressions.

    Canonical rule used by both code and evaluation labels:
    - Bare weekday ("Friday") and "this Friday" mean the first occurrence of
      that weekday strictly after the message timestamp.
    - "next Friday" means seven days after that first occurrence.
    - If the message is sent on Friday, "Friday" and "this Friday" therefore
      resolve to the following Friday, not the same calendar day.

    The input timestamp must be timezone-aware so the rule cannot silently move
    dates when the server and merchant use different timezones.
    """
    if message_timestamp.tzinfo is None:
        raise ContractError("message_timestamp must be timezone-aware")

    match = re.fullmatch(
        r"\s*(?:(this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*",
        phrase,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ContractError(f"Unsupported or ambiguous relative date: {phrase!r}")

    modifier = (match.group(1) or "").lower()
    target_weekday = WEEKDAYS[match.group(2).lower()]
    days_ahead = (target_weekday - message_timestamp.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    if modifier == "next":
        days_ahead += 7
    return message_timestamp + timedelta(days=days_ahead)


def resolve_relative_weekday_from_evidence(
    evidence_phrase: str, message_timestamp: datetime
) -> datetime:
    """Find exactly one relative weekday inside a larger verbatim span."""
    pattern = re.compile(
        r"\b(?:(?:this|next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(evidence_phrase))
    if len(matches) != 1:
        raise ContractError(
            "Date evidence must contain exactly one supported weekday expression"
        )
    return resolve_relative_weekday(matches[0].group(0), message_timestamp)
