import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CASES_PATH = (
    PROJECT_ROOT / "evals" / "workflow_safety_cases.json"
)

RESULT_PATH = (
    PROJECT_ROOT
    / "evals"
    / "results"
    / "workflow_safety_latest.json"
)

REPORT_PATH = (
    PROJECT_ROOT / "docs" / "SAFETY_REPORT.md"
)


def markdown_cell(value: object) -> str:
    return (
        str(value)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def testcase_status(
    testcase: ET.Element,
) -> tuple[str, str | None]:
    failure = testcase.find("failure")

    if failure is not None:
        return "FAILED", failure.text or failure.get("message")

    error = testcase.find("error")

    if error is not None:
        return "ERROR", error.text or error.get("message")

    skipped = testcase.find("skipped")

    if skipped is not None:
        return "SKIPPED", skipped.get("message")

    return "PASSED", None


def build_markdown_report(
    *,
    generated_at: str,
    results: list[dict[str, Any]],
    passed: int,
) -> str:
    total = len(results)
    pass_rate = passed / total if total else 0

    lines = [
        "# Workflow Safety Report",
        "",
        f"Generated: `{generated_at}`",
        "",
        "## Summary",
        "",
        f"- Frozen safety scenarios: **{total}**",
        f"- Passed: **{passed}**",
        f"- Failed or missing: **{total - passed}**",
        f"- Safety-control pass rate: **{pass_rate:.1%}**",
        "",
        "## Evaluated controls",
        "",
        "| Category | Control | Expected behavior | Result |",
        "|---|---|---|---|",
    ]

    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(result["category"]),
                    markdown_cell(result["control"]),
                    markdown_cell(result["expected"]),
                    markdown_cell(result["status"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "These scenarios test deterministic safety controls, "
                "state transitions, idempotency, webhook validation, "
                "reconciliation, and deadline handling."
            ),
            "",
            "This report does **not** claim real-world payment-recovery "
            "uplift. It demonstrates that the implemented recovery "
            "workflow behaves safely under the frozen scenarios.",
            "",
            "## Limitations",
            "",
            "- Razorpay is exercised in Test Mode, not with real money.",
            (
                "- Automated workflow tests use SQLite; PostgreSQL "
                "row locking is validated by implementation and manual "
                "integration testing, not concurrent load testing."
            ),
            (
                "- Groq extraction quality is evaluated separately "
                "using the frozen adversarial extraction dataset."
            ),
            (
                "- The current evaluation set is intentionally small "
                "and should be described as a safety smoke suite."
            ),
            "",
        ]
    )

    return "\n".join(lines)


def main() -> int:
    cases = json.loads(
        CASES_PATH.read_text(encoding="utf-8")
    )

    node_ids = [case["node_id"] for case in cases]

    with tempfile.TemporaryDirectory() as directory:
        junit_path = Path(directory) / "safety-results.xml"

        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *node_ids,
            f"--junitxml={junit_path}",
        ]

        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        print(completed.stdout)

        if completed.stderr:
            print(completed.stderr, file=sys.stderr)

        if not junit_path.exists():
            print(
                "Safety evaluation did not produce JUnit results.",
                file=sys.stderr,
            )
            return completed.returncode or 1

        tree = ET.parse(junit_path)

    testcases_by_name = {
        testcase.get("name"): testcase
        for testcase in tree.iter("testcase")
    }

    results: list[dict[str, Any]] = []

    for case in cases:
        test_name = case["node_id"].split("::")[-1]
        testcase = testcases_by_name.get(test_name)

        if testcase is None:
            status = "NOT_RUN"
            detail = "Test was not present in JUnit output"
            duration_seconds = None
        else:
            status, detail = testcase_status(testcase)
            duration_seconds = float(
                testcase.get("time", "0")
            )

        results.append(
            {
                **case,
                "status": status,
                "detail": detail,
                "duration_seconds": duration_seconds,
            }
        )

    passed = sum(
        result["status"] == "PASSED"
        for result in results
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    summary = {
        "generated_at": generated_at,
        "total_scenarios": len(results),
        "passed_scenarios": passed,
        "failed_or_missing_scenarios": (
            len(results) - passed
        ),
        "safety_control_pass_rate": round(
            passed / len(results) if results else 0,
            4,
        ),
    }

    RESULT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    RESULT_PATH.write_text(
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

    REPORT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    REPORT_PATH.write_text(
        build_markdown_report(
            generated_at=generated_at,
            results=results,
            passed=passed,
        ),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))
    print(f"Report: {REPORT_PATH}")

    if completed.returncode != 0:
        return completed.returncode

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())