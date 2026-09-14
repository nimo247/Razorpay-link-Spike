from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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


_RUPEE_AMOUNT_PATTERN = re.compile(
    r"(?<![\\w.])"
    r"(?P<prefix>₹|rs\\.?|inr)?\\s*"
    r"(?P<number>\\d[\\d,]*(?:\\.\\d+)?)\\s*"
    r"(?P<unit>k|thousand|lakh|lac|crore|cr)?"
    r"(?!\\w)",
    flags=re.IGNORECASE,
)

_RUPEE_MULTIPLIERS = {
    "": Decimal("1"),
    "k": Decimal("1000"),
    "thousand": Decimal("1000"),
    "lakh": Decimal("100000"),
    "lac": Decimal("100000"),
    "crore": Decimal("10000000"),
    "cr": Decimal("10000000"),
}

_COMMITMENT_UNCERTAINTY_PATTERNS = (
    re.compile(
        r"\\b(?:maybe|perhaps|possibly|probably|hopefully|might)\\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\\bshould\\s+be\\s+able\\s+to\\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\\b(?:i|we)(?:['’]ll|\\s+will)\\s+try\\s+to\\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\\bif\\b[^.!?]{0,160}"
        r"\\b(?:i(?:['’]ll|\\s+will)|we(?:['’]ll|\\s+will))"
        r"\\s+(?:pay|send|transfer)\\b",
        flags=re.IGNORECASE,
    ),
)


def extract_rupee_amounts_from_evidence(
    quotes: Iterable[str],
) -> tuple[int, ...]:
    """Normalize supported rupee expressions from grounded evidence.

    This parser is deliberately narrow. It does not decide which amount is a
    promise; it lets the Firewall verify that an LLM-proposed amount is
    actually represented by the verbatim evidence supplied with the proposal.
    """

    amounts: list[int] = []
    for quote in quotes:
        for match in _RUPEE_AMOUNT_PATTERN.finditer(quote):
            number_text = match.group("number").replace(",", "")
            unit = (match.group("unit") or "").lower()
            try:
                rupees = Decimal(number_text) * _RUPEE_MULTIPLIERS[unit]
            except (InvalidOperation, KeyError):
                continue
            paise = rupees * 100
            if paise > 0 and paise == paise.to_integral_value():
                amounts.append(int(paise))
    return tuple(amounts)


def commitment_language_requires_confirmation(message: str) -> bool:
    """Return True for explicit hedge or conditional commitment language."""

    return any(
        pattern.search(message)
        for pattern in _COMMITMENT_UNCERTAINTY_PATTERNS
    )


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
