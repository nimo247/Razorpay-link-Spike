from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from app.contracts import (
    ContractError,
    locate_exact_evidence,
    resolve_relative_weekday,
    resolve_relative_weekday_from_evidence,
)


IST = ZoneInfo("Asia/Kolkata")


class EvidenceContractTests(unittest.TestCase):
    def test_exact_source_quote_is_grounded(self) -> None:
        message = "I can pay 40k this Friday. The other 8k is disputed."
        spans = locate_exact_evidence(message, ["40k", "other 8k is disputed"])
        self.assertEqual([span.quote for span in spans], ["40k", "other 8k is disputed"])
        self.assertEqual(message[spans[0].start : spans[0].end], "40k")

    def test_reasonable_paraphrase_is_not_verbatim_evidence(self) -> None:
        message = "I can pay 40k this Friday."
        with self.assertRaises(ContractError):
            locate_exact_evidence(message, ["₹40,000"])

    def test_empty_quote_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            locate_exact_evidence("I can pay Friday", [""])


class RelativeDateContractTests(unittest.TestCase):
    def test_this_friday_means_first_friday_strictly_after_monday(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)  # Monday
        resolved = resolve_relative_weekday("this Friday", sent)
        self.assertEqual(resolved.date().isoformat(), "2026-09-04")

    def test_bare_friday_uses_same_rule_as_this_friday(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
        self.assertEqual(
            resolve_relative_weekday("Friday", sent),
            resolve_relative_weekday("this Friday", sent),
        )

    def test_same_day_is_strictly_future(self) -> None:
        sent = datetime(2026, 9, 4, 10, 0, tzinfo=IST)  # Friday
        resolved = resolve_relative_weekday("this Friday", sent)
        self.assertEqual(resolved.date().isoformat(), "2026-09-11")

    def test_next_friday_means_one_week_after_first_future_friday(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
        resolved = resolve_relative_weekday("next Friday", sent)
        self.assertEqual(resolved.date().isoformat(), "2026-09-11")

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            resolve_relative_weekday("Friday", datetime(2026, 8, 31, 10, 0))

    def test_weekday_isolated_from_by_friday(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
        resolved = resolve_relative_weekday_from_evidence("by Friday", sent)
        self.assertEqual(resolved.date().isoformat(), "2026-09-04")

    def test_weekday_isolated_from_sentence_fragment(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
        resolved = resolve_relative_weekday_from_evidence(
            "pay on next Friday please", sent
        )
        self.assertEqual(resolved.date().isoformat(), "2026-09-11")

    def test_multiple_weekdays_are_ambiguous(self) -> None:
        sent = datetime(2026, 8, 31, 10, 0, tzinfo=IST)
        with self.assertRaises(ContractError):
            resolve_relative_weekday_from_evidence("Friday or Monday", sent)


if __name__ == "__main__":
    unittest.main()
