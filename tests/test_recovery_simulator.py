import json
from pathlib import Path

from simulator.engine import (
    RecoverySimulator,
    SimulationState,
    StateTracker,
    load_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_PATH = PROJECT_ROOT / "simulator" / "personas_v1.json"
LOCK_PATH = PROJECT_ROOT / "simulator" / "personas_v1.lock.json"


def config():
    return load_config(PERSONAS_PATH, LOCK_PATH)


def run_small(episodes: int = 4):
    simulator = RecoverySimulator(config())
    try:
        return simulator.run(episodes_per_persona=episodes)
    finally:
        simulator.close()


def test_persona_set_is_locked_to_exactly_eight_synthetic_personas() -> None:
    loaded = config()
    raw = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))

    assert len(loaded.personas) == 8
    assert loaded.status == "FROZEN_SYNTHETIC_UNCALIBRATED"
    assert "not learned" in loaded.claim_boundary
    assert "reliability" not in json.dumps(raw).lower()


def test_seeded_simulation_is_exactly_reproducible() -> None:
    first = run_small()
    second = run_small()

    assert first["deterministic_result_sha256"] == (
        second["deterministic_result_sha256"]
    )
    assert first == second


def test_state_tracker_rejects_an_impossible_direct_payment_transition() -> None:
    tracker = StateTracker()

    try:
        tracker.transition(SimulationState.RECOVERED)
    except RuntimeError as error:
        assert "OUTSTANDING -> RECOVERED" in str(error)
    else:
        raise AssertionError("Impossible state transition was accepted")


def test_simulator_never_changes_payment_state_without_provider_truth() -> None:
    report = run_small(episodes=8)

    assert report["safety_gate"] == {
        "false_payment_state_changes_must_equal_zero": True,
        "policy_violations_must_equal_zero": True,
    }
    for metrics in report["policies"].values():
        assert metrics["false_payment_state_changes"] == 0
        assert metrics["policy_violations"] == 0
    assert all(
        trace["outcome"] == trace["final_state"]
        for trace in report["example_traces"]
    )


def test_adversarial_claims_are_blocked_by_the_real_firewall() -> None:
    report = run_small(episodes=30)
    aware = report["per_persona"]["adversarial_paid_claimant"][
        "PROMISE_AWARE_FIREWALL"
    ]

    assert aware["blocked_unsafe_actions"] > 0
    assert aware["false_payment_state_changes"] == 0


def test_partial_payer_exposes_exact_link_policy_difference() -> None:
    report = run_small(episodes=20)
    partial = report["per_persona"]["partial_payer"]

    assert (
        partial["GENERIC_EXACT_LINK"]["simulated_amount_recovery_rate"]
        == 0
    )
    assert (
        partial["PROMISE_AWARE_FIREWALL"]["simulated_amount_recovery_rate"]
        > 0
    )


def test_policies_receive_the_same_seeded_persona_tapes() -> None:
    report = run_small(episodes=2)
    traces = report["example_traces"]
    paired = {}
    for trace in traces:
        paired.setdefault(trace["persona_id"], set()).add(trace["seed"])

    assert len(paired) == 8
    assert all(len(seeds) == 1 for seeds in paired.values())


def test_report_cannot_be_mistaken_for_observed_recovery_data() -> None:
    report = run_small()

    assert report["report_type"] == "SYNTHETIC_SIMULATION_ONLY"
    assert report["real_world_claim"] is False
    assert "not an estimate" in (
        report["simulated_policy_delta"]["interpretation"]
    )
