from __future__ import annotations

import argparse
import json
import os
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
from evals.financial_action_harness import (  # noqa: E402
    evaluate_outputs,
    load_dataset,
    oracle_outputs,
    order_reproducibility,
    stability_metrics,
    validate_dataset,
)


DATASET_PATH = PROJECT_ROOT / "evals" / "financial_action_cases_v1.json"
LOCK_PATH = PROJECT_ROOT / "evals" / "financial_action_cases_v1.lock.json"
RESULTS_DIR = PROJECT_ROOT / "evals" / "results"
RUNS_DIR = PROJECT_ROOT / "evals" / "runs"
MAX_RATE_LIMIT_RETRIES = 5
RETRY_DELAY_PATTERN = re.compile(
    r"try again in ([0-9.]+)(ms|s)",
    re.IGNORECASE,
)


def extract_with_retry(case: dict[str, Any]):
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return extract_promise_with_groq(
                customer_message=case["customer_message"],
                outstanding_amount_paise=case["outstanding_amount_paise"],
            )
        except ExtractionError as error:
            message = str(error)
            if "HTTP 429" not in message or attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            match = RETRY_DELAY_PATTERN.search(message)
            if match:
                delay = float(match.group(1))
                if match.group(2).lower() == "ms":
                    delay /= 1000
                delay = max(delay + 1, 2)
            else:
                delay = min(15 * attempt, 60)
            print(
                f"Rate limited; waiting {delay:.1f}s before retry "
                f"{attempt + 1}/{MAX_RATE_LIMIT_RETRIES}."
            )
            time.sleep(delay)
    raise ExtractionError("Rate-limit retry loop ended unexpectedly")


def run_live(
    cases: list[dict[str, Any]],
    repetitions: int,
    delay_seconds: float,
) -> dict[str, Any]:
    attempts: dict[str, list[dict[str, Any]]] = {}
    for index, case in enumerate(cases, 1):
        attempts[case["id"]] = []
        for attempt in range(1, repetitions + 1):
            try:
                result = extract_with_retry(case)
            except ExtractionError as error:
                raise SystemExit(
                    f"Live extraction failed for {case['id']} attempt {attempt}: {error}"
                ) from error
            attempts[case["id"]].append(result.model_dump(mode="json"))
            print(
                f"[{index:03d}/{len(cases):03d}] {case['id']} "
                f"attempt {attempt}/{repetitions}"
            )
            if index != len(cases) or attempt != repetitions:
                time.sleep(delay_seconds)
    return {
        "run_type": "LIVE_GROQ",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repetitions": repetitions,
        "attempts": attempts,
    }


def load_run(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_immutable_run(run: dict[str, Any]) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"financial_actions_v1_{stamp}.json"
    if path.exists():
        raise SystemExit(f"Refusing to overwrite existing run: {path}")
    path.write_text(
        json.dumps(run, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        choices=("oracle", "live", "run"),
        default="oracle",
    )
    parser.add_argument("--run-file", type=Path)
    parser.add_argument("--stability", action="store_true")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=15,
        help="Delay between live calls; keep 15s for typical free-tier limits.",
    )
    args = parser.parse_args()

    dataset, digest = load_dataset(DATASET_PATH)
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if digest != lock["sha256"]:
        raise SystemExit("Dataset hash does not match the frozen V1 lock.")
    errors = validate_dataset(dataset, expected_hash=digest)
    if errors:
        raise SystemExit("Dataset validation failed:\n- " + "\n- ".join(errors))

    stability = None
    if args.source == "oracle":
        outputs = oracle_outputs(dataset)
        source_label = "ORACLE_PROPOSAL_CONTRACT_CHECK"
        result_name = "financial_action_oracle_v1.json"
    elif args.source == "run":
        if args.run_file is None:
            raise SystemExit("--run-file is required with --source run")
        run = load_run(args.run_file)
        if run.get("dataset_sha256") != digest:
            raise SystemExit(
                "Cached run does not declare the current frozen dataset hash."
            )
        attempts = run["attempts"]
        outputs = {
            case_id: values[0]
            for case_id, values in attempts.items()
            if values
        }
        source_label = run.get("run_type", "CACHED_RUN")
        result_name = f"{args.run_file.stem}_report.json"
        if args.stability:
            stability = stability_metrics(dataset, attempts)
    else:
        if not os.getenv("GROQ_API_KEY"):
            raise SystemExit(
                "GROQ_API_KEY is required; the label lock was not modified."
            )
        selected = (
            [case for case in dataset["cases"] if case["stability_subset"]]
            if args.stability
            else dataset["cases"]
        )
        repetitions = args.repetitions if args.stability else 1
        if args.stability and repetitions < 2:
            raise SystemExit("Stability runs require at least two repetitions.")
        live_started_at = datetime.now(timezone.utc).isoformat()
        if lock.get("first_live_model_run_at") is None:
            lock["first_live_model_run_at"] = live_started_at
            LOCK_PATH.write_text(
                json.dumps(lock, indent=2) + "\n",
                encoding="utf-8",
            )
        run = run_live(selected, repetitions, args.delay_seconds)
        run["dataset_sha256"] = digest
        run_path = write_immutable_run(run)
        attempts = run["attempts"]
        outputs = {
            case_id: values[0]
            for case_id, values in attempts.items()
            if values
        }
        source_label = "LIVE_GROQ"
        result_name = f"{run_path.stem}_report.json"
        if args.stability:
            stability = stability_metrics(dataset, attempts)

    evaluation_dataset = dataset
    if args.stability:
        evaluation_dataset = {
            **dataset,
            "cases": [
                case
                for case in dataset["cases"]
                if case["stability_subset"]
            ],
        }
    records, metrics = evaluate_outputs(evaluation_dataset, outputs)
    order_check = order_reproducibility(evaluation_dataset, outputs)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": digest,
        "source": source_label,
        "claim_boundary": (
            "Oracle results validate deterministic contracts only; live-model "
            "quality may be claimed only from a LIVE_GROQ run."
        ),
        "metrics": metrics,
        "order_reproducibility": order_check,
        "live_stability": stability,
        "results": records,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / result_name
    result_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, indent=2))
    print(json.dumps(order_check, indent=2))
    if stability is not None:
        print(stability["report_sentence"])
    else:
        print(
            "Live variance: NOT MEASURED in this run; no reproducibility claim "
            "is made for the model."
        )
    print(f"Report: {result_path}")
    if args.stability:
        return 0 if order_check["all_identical"] else 1
    return 0 if metrics["all_acceptance_targets_met"] and order_check["all_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
