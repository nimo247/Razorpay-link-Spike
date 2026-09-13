import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.promise_extractor import (  # noqa: E402
    ExtractionError,
    extract_promise_with_groq,
)


CASES_PATH = (
    PROJECT_ROOT / "evals" / "promise_extraction_cases.json"
)
RESULTS_PATH = (
    PROJECT_ROOT
    / "evals"
    / "results"
    / "promise_extraction_latest.json"
)

MAX_RATE_LIMIT_RETRIES = 5
DELAY_BETWEEN_CASES_SECONDS = 15

RETRY_DELAY_PATTERN = re.compile(
    r"try again in ([0-9.]+)(ms|s)",
    re.IGNORECASE,
)


def rate_limit_delay(
    error_message: str,
    attempt: int,
) -> float:
    match = RETRY_DELAY_PATTERN.search(error_message)

    if match:
        delay = float(match.group(1))
        unit = match.group(2).lower()

        if unit == "ms":
            delay /= 1000

        return max(delay + 1, 2)

    return min(15 * attempt, 60)


def extract_with_rate_limit_retry(
    *,
    customer_message: str,
    outstanding_amount_paise: int,
):
    for attempt in range(
        1,
        MAX_RATE_LIMIT_RETRIES + 1,
    ):
        try:
            return extract_promise_with_groq(
                customer_message=customer_message,
                outstanding_amount_paise=(
                    outstanding_amount_paise
                ),
            )

        except ExtractionError as error:
            message = str(error)

            if (
                "HTTP 429" not in message
                or attempt == MAX_RATE_LIMIT_RETRIES
            ):
                raise

            delay = rate_limit_delay(message, attempt)

            print(
                f"    Rate limited. Waiting "
                f"{delay:.1f} seconds before retry "
                f"{attempt + 1}/{MAX_RATE_LIMIT_RETRIES}..."
            )

            time.sleep(delay)

    raise ExtractionError(
        "Rate-limit retry loop ended unexpectedly"
    )


def compare_case(
    case: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    failures: list[str] = []

    for field, expected_value in case["expected"].items():
        actual_value = actual.get(field)

        if actual_value != expected_value:
            failures.append(
                f"{field}: expected {expected_value!r}, "
                f"received {actual_value!r}"
            )

    message = case["customer_message"]
    evidence_quotes = actual.get("evidence_quotes", [])

    for quote in evidence_quotes:
        if not quote or quote not in message:
            failures.append(
                f"ungrounded evidence quote: {quote!r}"
            )

    actionable_intents = {
        "FULL_PROMISE",
        "PARTIAL_PROMISE",
        "PARTIAL_PROMISE_WITH_DISPUTE",
        "DISPUTE_ONLY",
        "ALREADY_PAID",
    }

    if (
        actual.get("intent") in actionable_intents
        and not evidence_quotes
    ):
        failures.append(
            "actionable extraction contains no evidence quotes"
        )

    if (
        case.get("review_reason_required")
        and not actual.get("review_reason")
    ):
        failures.append(
            "review_reason must be present for this case"
        )

    return failures


def main() -> int:
    cases = json.loads(
        CASES_PATH.read_text(encoding="utf-8")
    )

    results: list[dict[str, Any]] = []

    passed_cases = 0
    failed_cases = 0
    error_cases = 0

    field_checks = 0
    passed_field_checks = 0

    evidence_quotes = 0
    grounded_evidence_quotes = 0

    for index, case in enumerate(cases, start=1):
        started_at = time.perf_counter()
        actual = None
        failures: list[str] = []
        infrastructure_error: str | None = None

        try:
            extraction = extract_with_rate_limit_retry(
                customer_message=case["customer_message"],
                outstanding_amount_paise=(
                    case["outstanding_amount_paise"]
                ),
            )

            actual = extraction.model_dump(mode="json")
            failures = compare_case(case, actual)

            for field, expected_value in case["expected"].items():
                field_checks += 1

                if actual.get(field) == expected_value:
                    passed_field_checks += 1

            for quote in actual.get("evidence_quotes", []):
                evidence_quotes += 1

                if quote and quote in case["customer_message"]:
                    grounded_evidence_quotes += 1

        except ExtractionError as error:
            infrastructure_error = str(error)

        elapsed_ms = round(
            (time.perf_counter() - started_at) * 1000,
            2,
        )

        if infrastructure_error:
            status = "ERROR"
            error_cases += 1
        elif failures:
            status = "FAIL"
            failed_cases += 1
        else:
            status = "PASS"
            passed_cases += 1

        print(
            f"[{index:02d}/{len(cases):02d}] "
            f"{status} {case['id']} ({elapsed_ms} ms)"
        )

        if infrastructure_error:
            print(
                f"    - Infrastructure error: "
                f"{infrastructure_error}"
            )

        for failure in failures:
            print(f"    - {failure}")

        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "status": status,
                "passed": status == "PASS",
                "latency_ms": elapsed_ms,
                "actual": actual,
                "failures": failures,
                "infrastructure_error": infrastructure_error,
            }
        )

        if index < len(cases):
            print(
                f"    Waiting "
                f"{DELAY_BETWEEN_CASES_SECONDS}s "
                f"for the free-tier TPM window..."
            )
            time.sleep(DELAY_BETWEEN_CASES_SECONDS)

    evaluated_cases = passed_cases + failed_cases

    case_accuracy = (
        passed_cases / evaluated_cases
        if evaluated_cases
        else 0
    )

    evaluation_coverage = (
        evaluated_cases / len(cases)
        if cases
        else 0
    )

    field_accuracy = (
        passed_field_checks / field_checks
        if field_checks
        else 0
    )

    evidence_grounding_rate = (
        grounded_evidence_quotes / evidence_quotes
        if evidence_quotes
        else 1
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(cases),
        "evaluated_cases": evaluated_cases,
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "infrastructure_errors": error_cases,
        "evaluation_coverage": round(
            evaluation_coverage,
            4,
        ),
        "case_accuracy": round(case_accuracy, 4),
        "field_accuracy": round(field_accuracy, 4),
        "evidence_grounding_rate": round(
            evidence_grounding_rate,
            4,
        ),
    }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "summary": summary,
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nSummary")
    print(json.dumps(summary, indent=2))
    print(f"\nSaved results to {RESULTS_PATH}")

    return 0 if failed_cases == 0 and error_cases == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())