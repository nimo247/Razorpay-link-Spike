from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from simulator.engine import (  # noqa: E402
    RecoverySimulator,
    load_config,
)


PERSONAS_PATH = PROJECT_ROOT / "simulator" / "personas_v1.json"
LOCK_PATH = PROJECT_ROOT / "simulator" / "personas_v1.lock.json"
RESULT_PATH = (
    PROJECT_ROOT / "evals" / "results" / "recovery_simulation_v1.json"
)
REPORT_PATH = PROJECT_ROOT / "docs" / "RECOVERY_SIMULATION_REPORT.md"


def percentage(value: float) -> str:
    return f"{value:.1%}"


def build_markdown(report: dict[str, Any], generated_at: str) -> str:
    policies = report["policies"]
    baseline = policies["GENERIC_EXACT_LINK"]
    aware = policies["PROMISE_AWARE_FIREWALL"]
    lines = [
        "# Recovery Simulator V1 Report",
        "",
        "> **SIMULATION ONLY — NOT A REAL-WORLD RECOVERY CLAIM**",
        "",
        f"Generated: `{generated_at}`",
        f"Deterministic result: `{report['deterministic_result_sha256']}`",
        "",
        "## Scope",
        "",
        f"- Synthetic personas: **{report['persona_count']}** (frozen at exactly eight)",
        f"- Episodes per persona and policy: **{report['episodes_per_persona']}**",
        f"- Total simulated episodes: **{report['total_episodes']}**",
        f"- Fixed seed: **{report['seed']}**",
        f"- Horizon: **{report['horizon_days']} simulated days**",
        "- Historical calibration: **none**",
        "",
        "All behavior probabilities are fictional test assumptions. The results",
        "demonstrate state-machine and policy behavior under those assumptions;",
        "they do not estimate merchant revenue, collections performance, or",
        "Razorpay recovery uplift.",
        "",
        "## Simulated comparison",
        "",
        "| Metric | Generic exact-link reminders | Promise-aware Firewall |",
        "|---|---:|---:|",
        (
            "| Simulated amount recovery | "
            f"{percentage(baseline['simulated_amount_recovery_rate'])} | "
            f"{percentage(aware['simulated_amount_recovery_rate'])} |"
        ),
        (
            "| Full recovery | "
            f"{percentage(baseline['full_recovery_rate'])} | "
            f"{percentage(aware['full_recovery_rate'])} |"
        ),
        (
            "| Any recovery | "
            f"{percentage(baseline['any_recovery_rate'])} | "
            f"{percentage(aware['any_recovery_rate'])} |"
        ),
        (
            "| Average contacts | "
            f"{baseline['average_contacts']:.3f} | "
            f"{aware['average_contacts']:.3f} |"
        ),
        (
            "| Human escalation | "
            f"{percentage(baseline['human_escalation_rate'])} | "
            f"{percentage(aware['human_escalation_rate'])} |"
        ),
        (
            "| Confirmations per episode | "
            f"{baseline['confirmation_burden_per_episode']:.3f} | "
            f"{aware['confirmation_burden_per_episode']:.3f} |"
        ),
        (
            "| Blocked unverified paid claims | "
            f"{baseline['blocked_unsafe_actions']} | "
            f"{aware['blocked_unsafe_actions']} |"
        ),
        (
            "| False payment-state changes | "
            f"{baseline['false_payment_state_changes']} | "
            f"{aware['false_payment_state_changes']} |"
        ),
        "",
        "The delta is intentionally named **simulated policy delta**, not uplift:",
        "",
        (
            "- Amount-recovery-rate delta: "
            f"**{report['simulated_policy_delta']['promise_aware_minus_baseline_amount_recovery_rate']:+.1%}**"
        ),
        (
            "- Average-contact delta: "
            f"**{report['simulated_policy_delta']['promise_aware_minus_baseline_average_contacts']:+.3f}**"
        ),
        "",
        "## Per-persona amount recovery",
        "",
        "| Persona | Generic reminders | Promise-aware Firewall |",
        "|---|---:|---:|",
    ]
    for persona_id, metrics in report["per_persona"].items():
        lines.append(
            f"| `{persona_id}` | "
            f"{percentage(metrics['GENERIC_EXACT_LINK']['simulated_amount_recovery_rate'])} | "
            f"{percentage(metrics['PROMISE_AWARE_FIREWALL']['simulated_amount_recovery_rate'])} |"
        )
    lines.extend([
        "",
        "## Safety result",
        "",
        f"- False payment-state changes: **{aware['false_payment_state_changes'] + baseline['false_payment_state_changes']}**",
        f"- Policy violations: **{aware['policy_violations'] + baseline['policy_violations']}**",
        f"- Unverified paid claims blocked by the promise-aware policy: **{aware['blocked_unsafe_actions']}**",
        "",
        "A simulated payment changes the invoice balance only after the harness",
        "persists a provider event and calls the production `apply_exact_payment`",
        "path, which invokes the Financial Action Firewall. Customer claims alone",
        "cannot mutate the balance.",
        "",
        "## Deliberate exclusions",
        "",
        "- No promise-reliability model",
        "- No multi-turn negotiation",
        "- No contextual bandit",
        "- No omnichannel delivery",
        "- No real customer or repayment data",
        "- No claim of causal or production recovery improvement",
        "",
    ])
    return "\n".join(lines)


def run_once(episodes: int | None) -> dict[str, Any]:
    config = load_config(PERSONAS_PATH, LOCK_PATH)
    simulator = RecoverySimulator(config)
    try:
        return simulator.run(episodes_per_persona=episodes)
    finally:
        simulator.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--episodes-per-persona",
        type=int,
        help="Testing override; the committed V1 report uses the frozen value.",
    )
    parser.add_argument(
        "--skip-reproducibility-check",
        action="store_true",
    )
    args = parser.parse_args()
    if args.episodes_per_persona is not None and args.episodes_per_persona <= 0:
        raise SystemExit("episodes-per-persona must be positive")

    report = run_once(args.episodes_per_persona)
    if not args.skip_reproducibility_check:
        replay = run_once(args.episodes_per_persona)
        if (
            replay["deterministic_result_sha256"]
            != report["deterministic_result_sha256"]
        ):
            raise SystemExit("Seeded replay produced a different result")

    if not all(report["safety_gate"].values()):
        raise SystemExit("Simulation safety gate failed")

    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {"generated_at": generated_at, **report}
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    REPORT_PATH.write_text(
        build_markdown(report, generated_at),
        encoding="utf-8",
    )
    print(json.dumps({
        "result_type": report["report_type"],
        "personas": report["persona_count"],
        "total_episodes": report["total_episodes"],
        "deterministic_result_sha256": report["deterministic_result_sha256"],
        "safety_gate": report["safety_gate"],
        "simulated_policy_delta": report["simulated_policy_delta"],
    }, indent=2))
    print(f"Report: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
