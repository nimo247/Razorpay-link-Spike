from __future__ import annotations

import hashlib
import json
import os
import random
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from statistics import mean
from typing import Any


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from app.services.deadline_worker import mark_broken_promises  # noqa: E402
from app.services.financial_action_firewall import (  # noqa: E402
    ActionProposal,
    FinancialAction,
    FinancialActionFirewall,
    FirewallDecisionStatus,
)
from app.services.payment_application import apply_exact_payment  # noqa: E402


SIMULATION_START_DATE = date(2026, 9, 14)
RESPONSE_ORDER = (
    "PROMISE",
    "DIRECT_PAYMENT",
    "DISPUTE",
    "AMBIGUOUS",
    "FALSE_PAID_CLAIM",
    "NO_ACTION",
)


class SimulationPolicy(str, Enum):
    GENERIC_EXACT_LINK = "GENERIC_EXACT_LINK"
    PROMISE_AWARE_FIREWALL = "PROMISE_AWARE_FIREWALL"


class EpisodeOutcome(str, Enum):
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    DISPUTED = "DISPUTED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BROKEN_PROMISE = "BROKEN_PROMISE"
    OPEN_COMMITMENT = "OPEN_COMMITMENT"
    EXHAUSTED = "EXHAUSTED"


class SimulationState(str, Enum):
    OUTSTANDING = "OUTSTANDING"
    CONTACTED = "CONTACTED"
    LINK_CREATED = "LINK_CREATED"
    CLAIM_BLOCKED = "CLAIM_BLOCKED"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    DISPUTED = "DISPUTED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BROKEN_PROMISE = "BROKEN_PROMISE"
    OPEN_COMMITMENT = "OPEN_COMMITMENT"
    EXHAUSTED = "EXHAUSTED"


ALLOWED_TRANSITIONS = {
    SimulationState.OUTSTANDING: {SimulationState.CONTACTED},
    SimulationState.CONTACTED: {
        SimulationState.CONTACTED,
        SimulationState.LINK_CREATED,
        SimulationState.DISPUTED,
        SimulationState.HUMAN_REVIEW,
        SimulationState.OPEN_COMMITMENT,
        SimulationState.EXHAUSTED,
    },
    SimulationState.LINK_CREATED: {
        SimulationState.PAYMENT_VERIFIED,
        SimulationState.CLAIM_BLOCKED,
        SimulationState.HUMAN_REVIEW,
        SimulationState.BROKEN_PROMISE,
        SimulationState.OPEN_COMMITMENT,
    },
    SimulationState.CLAIM_BLOCKED: {
        SimulationState.CONTACTED,
        SimulationState.EXHAUSTED,
    },
    SimulationState.PAYMENT_VERIFIED: {
        SimulationState.RECOVERED,
        SimulationState.PARTIALLY_RECOVERED,
    },
}


class StateTracker:
    def __init__(self) -> None:
        self.state = SimulationState.OUTSTANDING

    def transition(self, next_state: SimulationState) -> None:
        allowed = ALLOWED_TRANSITIONS.get(self.state, set())
        if next_state not in allowed:
            raise RuntimeError(
                f"Invalid simulated transition: {self.state.value} -> "
                f"{next_state.value}"
            )
        self.state = next_state


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    description: str
    invoice_amount_paise: int
    response_probability: float
    response_mix: dict[str, float]
    payment_fraction: float
    promise_keep_probability: float
    interpretation_success_probability: float
    promise_due_days: int


@dataclass(frozen=True)
class SimulatorConfig:
    version: str
    status: str
    claim_boundary: str
    seed: int
    horizon_days: int
    max_contacts: int
    contact_interval_days: int
    episodes_per_persona: int
    personas: tuple[Persona, ...]
    sha256: str


@dataclass(frozen=True)
class ContactDraw:
    response: float
    behavior: float
    interpretation: float
    promise_kept: float


@dataclass(frozen=True)
class EpisodeResult:
    policy: str
    persona_id: str
    episode: int
    seed: int
    outcome: str
    final_state: str
    original_amount_paise: int
    recovered_amount_paise: int
    outstanding_amount_paise: int
    contacts: int
    confirmations: int
    human_escalations: int
    blocked_unsafe_actions: int
    false_payment_state_changes: int
    policy_violations: int
    promises_created: int
    promises_broken: int
    resolution_day: int | None
    trace: tuple[dict[str, Any], ...]


def load_config(personas_path: Path, lock_path: Path) -> SimulatorConfig:
    raw = personas_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if digest != lock.get("sha256"):
        raise ValueError("Persona configuration does not match its frozen lock")
    if payload.get("status") != "FROZEN_SYNTHETIC_UNCALIBRATED":
        raise ValueError("Persona configuration must remain explicitly synthetic")
    persona_payloads = payload.get("personas", [])
    if len(persona_payloads) != 8 or lock.get("persona_count") != 8:
        raise ValueError("Simulator V1 must contain exactly eight personas")
    if len({item["id"] for item in persona_payloads}) != 8:
        raise ValueError("Persona IDs must be unique")

    personas = []
    for item in persona_payloads:
        mix = item["response_mix"]
        if set(mix) != set(RESPONSE_ORDER):
            raise ValueError(f"{item['id']}: response mix keys are incomplete")
        if abs(sum(mix.values()) - 1.0) > 1e-9:
            raise ValueError(f"{item['id']}: response mix must sum to one")
        probabilities = [
            item["response_probability"],
            item["payment_fraction"],
            item["promise_keep_probability"],
            item["interpretation_success_probability"],
            *mix.values(),
        ]
        if any(value < 0 or value > 1 for value in probabilities):
            raise ValueError(f"{item['id']}: probability outside [0, 1]")
        personas.append(Persona(**item))

    return SimulatorConfig(
        version=payload["persona_set_version"],
        status=payload["status"],
        claim_boundary=payload["claim_boundary"],
        seed=payload["seed"],
        horizon_days=payload["horizon_days"],
        max_contacts=payload["max_contacts"],
        contact_interval_days=payload["contact_interval_days"],
        episodes_per_persona=payload["episodes_per_persona"],
        personas=tuple(personas),
        sha256=digest,
    )


def episode_seed(global_seed: int, persona_id: str, episode: int) -> int:
    material = f"{global_seed}:{persona_id}:{episode}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def build_tape(config: SimulatorConfig, persona: Persona, episode: int) -> tuple[int, tuple[ContactDraw, ...]]:
    seed = episode_seed(config.seed, persona.id, episode)
    rng = random.Random(seed)
    draws = tuple(
        ContactDraw(
            response=rng.random(),
            behavior=rng.random(),
            interpretation=rng.random(),
            promise_kept=rng.random(),
        )
        for _ in range(config.max_contacts)
    )
    return seed, draws


def response_kind(persona: Persona, draw: ContactDraw) -> str:
    if draw.response >= persona.response_probability:
        return "NO_RESPONSE"
    cumulative = 0.0
    for name in RESPONSE_ORDER:
        cumulative += persona.response_mix[name]
        if draw.behavior < cumulative:
            return name
    return "NO_ACTION"


class EpisodeLedger:
    def __init__(
        self,
        session: Session,
        persona: Persona,
        episode_key: str,
    ) -> None:
        self.session = session
        self.persona = persona
        self.episode_key = episode_key
        self.invoice = Invoice(
            customer_name=f"Synthetic {persona.label}",
            original_amount_paise=persona.invoice_amount_paise,
            paid_amount_paise=0,
            disputed_amount_paise=0,
            outstanding_amount_paise=persona.invoice_amount_paise,
            due_date=SIMULATION_START_DATE - timedelta(days=10),
            status=InvoiceStatus.OVERDUE,
        )
        session.add(self.invoice)
        session.flush()
        self.promise: PaymentPromise | None = None
        self.provider_event_ids: list[str] = []

    def _firewall(self) -> FinancialActionFirewall:
        return FinancialActionFirewall(
            self.session,
            today_provider=lambda: SIMULATION_START_DATE,
        )

    def create_customer_link(
        self,
        *,
        amount_paise: int,
        due_day: int,
    ) -> tuple[bool, int, str]:
        if self.promise is not None:
            if (
                self.promise.status == PromiseStatus.LINK_CREATED
                and self.promise.promised_amount_paise == amount_paise
            ):
                return True, 0, "EXISTING_EXACT_LINK"
            return False, 0, "EXISTING_LINK_AMOUNT_MISMATCH"
        message = f"I will pay {amount_paise} paise on Friday."
        evidence = (str(amount_paise), "Friday")
        due_date = SIMULATION_START_DATE + timedelta(days=due_day)
        unconfirmed = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.CREATE_COMMITMENT,
                invoice_id=self.invoice.id,
                amount_paise=amount_paise,
                customer_message=message,
                evidence_quotes=evidence,
                promised_date=due_date,
                human_confirmed=False,
                actor="SIMULATED_LLM",
            )
        )
        if unconfirmed.status != FirewallDecisionStatus.REQUIRES_CONFIRMATION:
            return False, 0, unconfirmed.rule.value
        confirmed = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.CREATE_COMMITMENT,
                invoice_id=self.invoice.id,
                amount_paise=amount_paise,
                customer_message=message,
                evidence_quotes=evidence,
                promised_date=due_date,
                human_confirmed=True,
                actor="SIMULATED_MERCHANT",
            )
        )
        if not confirmed.authorized:
            return False, 1, confirmed.rule.value
        self.promise = PaymentPromise(
            invoice_id=self.invoice.id,
            customer_message=message,
            promised_amount_paise=amount_paise,
            disputed_amount_paise=0,
            promised_date=due_date,
            evidence_quotes=list(evidence),
            status=PromiseStatus.VALIDATED,
        )
        self.session.add(self.promise)
        self.session.flush()
        link_id = f"plink_sim_{self.episode_key}"
        link_decision = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.CREATE_PAYMENT_LINK,
                invoice_id=self.invoice.id,
                promise_id=self.promise.id,
                amount_paise=amount_paise,
                currency="INR",
                human_confirmed=True,
                actor="SIMULATOR",
            )
        )
        if not link_decision.authorized:
            return False, 1, link_decision.rule.value
        self.promise.status = PromiseStatus.LINK_CREATED
        self.promise.payment_link_id = link_id
        self.promise.payment_link_url = f"https://rzp.io/sim/{link_id}"
        self.session.flush()
        return True, 1, "ALL_GUARDRAILS_PASSED"

    def create_generic_link(self) -> tuple[bool, str]:
        if self.promise is not None:
            return True, "EXISTING_LINK"
        amount = self.invoice.outstanding_amount_paise
        self.promise = PaymentPromise(
            invoice_id=self.invoice.id,
            customer_message="Synthetic generic exact-balance link fixture",
            promised_amount_paise=amount,
            disputed_amount_paise=0,
            promised_date=SIMULATION_START_DATE + timedelta(days=21),
            evidence_quotes=["exact-balance"],
            status=PromiseStatus.VALIDATED,
        )
        self.session.add(self.promise)
        self.session.flush()
        decision = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.CREATE_PAYMENT_LINK,
                invoice_id=self.invoice.id,
                promise_id=self.promise.id,
                amount_paise=amount,
                currency="INR",
                actor="SIMULATED_BASELINE_FIXTURE",
            )
        )
        if not decision.authorized:
            return False, decision.rule.value
        link_id = f"plink_sim_{self.episode_key}"
        self.promise.status = PromiseStatus.LINK_CREATED
        self.promise.payment_link_id = link_id
        self.promise.payment_link_url = f"https://rzp.io/sim/{link_id}"
        self.session.flush()
        return True, "ALL_GUARDRAILS_PASSED"

    def register_dispute(self) -> tuple[bool, int, str]:
        message = "I dispute the outstanding balance."
        evidence = ("dispute the outstanding balance",)
        amount = self.invoice.outstanding_amount_paise
        unconfirmed = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.REGISTER_DISPUTE,
                invoice_id=self.invoice.id,
                amount_paise=amount,
                customer_message=message,
                evidence_quotes=evidence,
                human_confirmed=False,
                actor="SIMULATED_LLM",
            )
        )
        if unconfirmed.status != FirewallDecisionStatus.REQUIRES_CONFIRMATION:
            return False, 0, unconfirmed.rule.value
        confirmed = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.REGISTER_DISPUTE,
                invoice_id=self.invoice.id,
                amount_paise=amount,
                customer_message=message,
                evidence_quotes=evidence,
                human_confirmed=True,
                actor="SIMULATED_MERCHANT",
            )
        )
        if confirmed.authorized:
            self.invoice.disputed_amount_paise = amount
            self.invoice.status = InvoiceStatus.DISPUTED
            self.session.flush()
        return confirmed.authorized, 1, confirmed.rule.value

    def block_unverified_paid_claim(self) -> tuple[bool, str]:
        linked, rule = self.create_generic_link()
        if not linked or self.promise is None:
            return False, rule
        paid_before = self.invoice.paid_amount_paise
        decision = self._firewall().authorize(
            ActionProposal(
                action=FinancialAction.MARK_PAID,
                invoice_id=self.invoice.id,
                promise_id=self.promise.id,
                amount_paise=self.promise.promised_amount_paise,
                currency="INR",
                payment_link_id=self.promise.payment_link_id,
                provider_event_id=None,
                human_confirmed=True,
                actor="SIMULATED_CUSTOMER_CLAIM",
            )
        )
        unchanged = self.invoice.paid_amount_paise == paid_before
        return decision.status == FirewallDecisionStatus.BLOCKED and unchanged, decision.rule.value

    def apply_provider_payment(self, day: int) -> tuple[bool, str]:
        if self.promise is None or self.promise.payment_link_id is None:
            return False, "NO_LINKED_PROMISE"
        event_id = f"evt_sim_{self.episode_key}_{day}"
        amount = self.promise.promised_amount_paise
        event = WebhookEvent(
            event_id=event_id,
            event_type="payment_link.paid",
            payload_sha256=hashlib.sha256(event_id.encode()).hexdigest(),
            payload={
                "event": "payment_link.paid",
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": self.promise.payment_link_id,
                            "amount": amount,
                            "amount_paid": amount,
                            "currency": "INR",
                            "status": "paid",
                        }
                    }
                },
            },
        )
        self.session.add(event)
        self.session.flush()
        result = apply_exact_payment(
            self.session,
            invoice=self.invoice,
            payment_promise=self.promise,
            payment_link_id=self.promise.payment_link_id,
            amount_paid=amount,
            source="WEBHOOK",
            razorpay_event_id=event_id,
        )
        self.provider_event_ids.append(event_id)
        self.session.flush()
        return not bool(result["human_review"]), str(result.get("reason") or "APPLIED")

    def mark_broken(self, due_day: int) -> bool:
        if self.promise is None:
            return False
        broken = mark_broken_promises(
            self.session,
            as_of=SIMULATION_START_DATE + timedelta(days=due_day + 1),
        )
        self.session.flush()
        return self.promise.id in broken

    def false_payment_state_changes(self) -> int:
        provider_count = self.session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(WebhookEvent.event_id.in_(self.provider_event_ids))
        ) if self.provider_event_ids else 0
        return int(self.invoice.paid_amount_paise > 0 and provider_count == 0)


class RecoverySimulator:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)

    def close(self) -> None:
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def run(
        self,
        *,
        episodes_per_persona: int | None = None,
    ) -> dict[str, Any]:
        episodes = episodes_per_persona or self.config.episodes_per_persona
        results: list[EpisodeResult] = []
        for persona in self.config.personas:
            for episode in range(episodes):
                seed, tape = build_tape(self.config, persona, episode)
                for policy in SimulationPolicy:
                    with Session(self.engine, expire_on_commit=False) as session:
                        ledger = EpisodeLedger(
                            session,
                            persona,
                            f"{policy.value.lower()}_{persona.id}_{episode}",
                        )
                        if policy == SimulationPolicy.GENERIC_EXACT_LINK:
                            result = self._run_baseline(
                                session, ledger, persona, episode, seed, tape
                            )
                        else:
                            result = self._run_promise_aware(
                                session, ledger, persona, episode, seed, tape
                            )
                        session.commit()
                        results.append(result)
        return build_report(self.config, results, episodes)

    def _run_baseline(
        self,
        session: Session,
        ledger: EpisodeLedger,
        persona: Persona,
        episode: int,
        seed: int,
        tape: tuple[ContactDraw, ...],
    ) -> EpisodeResult:
        trace: list[dict[str, Any]] = []
        tracker = StateTracker()
        contacts = 0
        resolution_day = None
        for index, draw in enumerate(tape):
            day = index * self.config.contact_interval_days
            contacts += 1
            tracker.transition(SimulationState.CONTACTED)
            kind = response_kind(persona, draw)
            self._trace(trace, episode, day, "CONTACT", response=kind)
            pays = kind == "DIRECT_PAYMENT" or (
                kind == "PROMISE"
                and draw.promise_kept < persona.promise_keep_probability
            )
            if not pays:
                continue
            intended = max(
                1,
                round(persona.invoice_amount_paise * persona.payment_fraction),
            )
            if intended != ledger.invoice.outstanding_amount_paise:
                self._trace(
                    trace,
                    episode,
                    day,
                    "PARTIAL_AMOUNT_UNSUPPORTED_BY_GENERIC_EXACT_LINK",
                    intended_amount_paise=intended,
                )
                continue
            payment_day = day + (persona.promise_due_days if kind == "PROMISE" else 0)
            if payment_day > self.config.horizon_days:
                tracker.transition(SimulationState.OPEN_COMMITMENT)
                return self._result(
                    SimulationPolicy.GENERIC_EXACT_LINK,
                    persona,
                    episode,
                    seed,
                    EpisodeOutcome.OPEN_COMMITMENT,
                    ledger,
                    contacts,
                    tracker=tracker,
                    resolution_day=None,
                    trace=trace,
                )
            linked, rule = ledger.create_generic_link()
            if not linked:
                self._trace(trace, episode, day, "LINK_BLOCKED", rule=rule)
                continue
            tracker.transition(SimulationState.LINK_CREATED)
            applied, reason = ledger.apply_provider_payment(payment_day)
            self._trace(
                trace,
                episode,
                payment_day,
                "PROVIDER_PAYMENT",
                applied=applied,
                reason=reason,
            )
            if applied:
                tracker.transition(SimulationState.PAYMENT_VERIFIED)
                tracker.transition(SimulationState.RECOVERED)
                resolution_day = payment_day
                break
        outcome = (
            EpisodeOutcome.RECOVERED
            if ledger.invoice.outstanding_amount_paise == 0
            else EpisodeOutcome.EXHAUSTED
        )
        if outcome == EpisodeOutcome.EXHAUSTED:
            tracker.transition(SimulationState.EXHAUSTED)
        return self._result(
            SimulationPolicy.GENERIC_EXACT_LINK,
            persona,
            episode,
            seed,
            outcome,
            ledger,
            contacts,
            tracker=tracker,
            resolution_day=resolution_day,
            trace=trace,
        )

    def _run_promise_aware(
        self,
        session: Session,
        ledger: EpisodeLedger,
        persona: Persona,
        episode: int,
        seed: int,
        tape: tuple[ContactDraw, ...],
    ) -> EpisodeResult:
        trace: list[dict[str, Any]] = []
        tracker = StateTracker()
        contacts = 0
        confirmations = 0
        escalations = 0
        blocked_unsafe = 0
        policy_violations = 0
        promises_created = 0
        promises_broken = 0
        resolution_day = None
        outcome = EpisodeOutcome.EXHAUSTED

        for index, draw in enumerate(tape):
            day = index * self.config.contact_interval_days
            contacts += 1
            tracker.transition(SimulationState.CONTACTED)
            kind = response_kind(persona, draw)
            self._trace(trace, episode, day, "CONTACT", response=kind)
            if kind in {"NO_RESPONSE", "NO_ACTION"}:
                continue
            if kind == "FALSE_PAID_CLAIM":
                blocked, rule = ledger.block_unverified_paid_claim()
                tracker.transition(SimulationState.LINK_CREATED)
                blocked_unsafe += int(blocked)
                policy_violations += int(not blocked)
                tracker.transition(
                    SimulationState.CLAIM_BLOCKED
                    if blocked
                    else SimulationState.HUMAN_REVIEW
                )
                self._trace(
                    trace,
                    episode,
                    day,
                    "UNVERIFIED_PAID_CLAIM",
                    blocked=blocked,
                    rule=rule,
                )
                if not blocked:
                    outcome = EpisodeOutcome.HUMAN_REVIEW
                    break
                continue
            if kind == "DISPUTE":
                authorized, count, rule = ledger.register_dispute()
                confirmations += count
                escalations += 1
                outcome = (
                    EpisodeOutcome.DISPUTED
                    if authorized
                    else EpisodeOutcome.HUMAN_REVIEW
                )
                tracker.transition(
                    SimulationState.DISPUTED
                    if authorized
                    else SimulationState.HUMAN_REVIEW
                )
                self._trace(
                    trace,
                    episode,
                    day,
                    "DISPUTE_ROUTED",
                    authorized=authorized,
                    rule=rule,
                )
                break
            if kind == "AMBIGUOUS" or (
                draw.interpretation
                >= persona.interpretation_success_probability
            ):
                confirmations += 1
                escalations += 1
                outcome = EpisodeOutcome.HUMAN_REVIEW
                tracker.transition(SimulationState.HUMAN_REVIEW)
                self._trace(trace, episode, day, "HUMAN_REVIEW_REQUIRED")
                break

            intended = max(
                1,
                round(persona.invoice_amount_paise * persona.payment_fraction),
            )
            payment_day = day + (
                persona.promise_due_days if kind == "PROMISE" else 0
            )
            linked, count, rule = ledger.create_customer_link(
                amount_paise=intended,
                due_day=payment_day,
            )
            confirmations += count
            promises_created += int(linked and rule != "EXISTING_EXACT_LINK")
            self._trace(
                trace,
                episode,
                day,
                "EXACT_COMMITMENT_LINK",
                authorized=linked,
                amount_paise=intended,
                rule=rule,
            )
            if not linked:
                escalations += 1
                outcome = EpisodeOutcome.HUMAN_REVIEW
                tracker.transition(SimulationState.HUMAN_REVIEW)
                break
            tracker.transition(SimulationState.LINK_CREATED)
            if payment_day > self.config.horizon_days:
                outcome = EpisodeOutcome.OPEN_COMMITMENT
                tracker.transition(SimulationState.OPEN_COMMITMENT)
                break
            pays = kind == "DIRECT_PAYMENT" or (
                draw.promise_kept < persona.promise_keep_probability
            )
            if pays:
                applied, reason = ledger.apply_provider_payment(payment_day)
                self._trace(
                    trace,
                    episode,
                    payment_day,
                    "PROVIDER_PAYMENT",
                    applied=applied,
                    reason=reason,
                )
                if applied:
                    tracker.transition(SimulationState.PAYMENT_VERIFIED)
                    resolution_day = payment_day
                    outcome = (
                        EpisodeOutcome.RECOVERED
                        if ledger.invoice.outstanding_amount_paise == 0
                        else EpisodeOutcome.PARTIALLY_RECOVERED
                    )
                    tracker.transition(SimulationState(outcome.value))
                else:
                    escalations += 1
                    outcome = EpisodeOutcome.HUMAN_REVIEW
                    tracker.transition(SimulationState.HUMAN_REVIEW)
                break
            if rule == "EXISTING_EXACT_LINK":
                outcome = EpisodeOutcome.OPEN_COMMITMENT
                tracker.transition(SimulationState.OPEN_COMMITMENT)
                self._trace(
                    trace,
                    episode,
                    payment_day,
                    "EXISTING_LINK_REMAINS_OPEN",
                )
                break
            broken = ledger.mark_broken(payment_day)
            promises_broken += int(broken)
            policy_violations += int(not broken)
            outcome = EpisodeOutcome.BROKEN_PROMISE
            tracker.transition(SimulationState.BROKEN_PROMISE)
            self._trace(
                trace,
                episode,
                payment_day + 1,
                "PROMISE_BROKEN",
                transitioned=broken,
            )
            break

        if outcome == EpisodeOutcome.EXHAUSTED:
            tracker.transition(SimulationState.EXHAUSTED)

        return self._result(
            SimulationPolicy.PROMISE_AWARE_FIREWALL,
            persona,
            episode,
            seed,
            outcome,
            ledger,
            contacts,
            tracker=tracker,
            confirmations=confirmations,
            human_escalations=escalations,
            blocked_unsafe_actions=blocked_unsafe,
            additional_policy_violations=policy_violations,
            promises_created=promises_created,
            promises_broken=promises_broken,
            resolution_day=resolution_day,
            trace=trace,
        )

    @staticmethod
    def _trace(
        trace: list[dict[str, Any]],
        episode: int,
        day: int,
        event: str,
        **detail: Any,
    ) -> None:
        if episode == 0:
            trace.append({"day": day, "event": event, **detail})

    @staticmethod
    def _result(
        policy: SimulationPolicy,
        persona: Persona,
        episode: int,
        seed: int,
        outcome: EpisodeOutcome,
        ledger: EpisodeLedger,
        contacts: int,
        *,
        tracker: StateTracker,
        confirmations: int = 0,
        human_escalations: int = 0,
        blocked_unsafe_actions: int = 0,
        additional_policy_violations: int = 0,
        promises_created: int = 0,
        promises_broken: int = 0,
        resolution_day: int | None,
        trace: list[dict[str, Any]],
    ) -> EpisodeResult:
        if tracker.state.value != outcome.value:
            raise RuntimeError(
                f"Final state {tracker.state.value} does not match outcome "
                f"{outcome.value}"
            )
        false_changes = ledger.false_payment_state_changes()
        policy_violations = (
            additional_policy_violations + int(false_changes > 0)
        )
        recovered = (
            ledger.invoice.original_amount_paise
            - ledger.invoice.outstanding_amount_paise
        )
        return EpisodeResult(
            policy=policy.value,
            persona_id=persona.id,
            episode=episode,
            seed=seed,
            outcome=outcome.value,
            final_state=tracker.state.value,
            original_amount_paise=ledger.invoice.original_amount_paise,
            recovered_amount_paise=recovered,
            outstanding_amount_paise=ledger.invoice.outstanding_amount_paise,
            contacts=contacts,
            confirmations=confirmations,
            human_escalations=human_escalations,
            blocked_unsafe_actions=blocked_unsafe_actions,
            false_payment_state_changes=false_changes,
            policy_violations=policy_violations,
            promises_created=promises_created,
            promises_broken=promises_broken,
            resolution_day=resolution_day,
            trace=tuple(trace),
        )


def aggregate(results: list[EpisodeResult]) -> dict[str, Any]:
    total = len(results)
    original = sum(item.original_amount_paise for item in results)
    recovered = sum(item.recovered_amount_paise for item in results)
    recovered_results = [item for item in results if item.recovered_amount_paise > 0]
    resolution_days = [
        item.resolution_day
        for item in recovered_results
        if item.resolution_day is not None
    ]
    return {
        "episodes": total,
        "simulated_original_amount_paise": original,
        "simulated_recovered_amount_paise": recovered,
        "simulated_amount_recovery_rate": round(recovered / original, 4) if original else 0,
        "full_recovery_rate": round(
            sum(item.outcome == EpisodeOutcome.RECOVERED.value for item in results) / total,
            4,
        ) if total else 0,
        "any_recovery_rate": round(len(recovered_results) / total, 4) if total else 0,
        "partial_recovery_rate": round(
            sum(item.outcome == EpisodeOutcome.PARTIALLY_RECOVERED.value for item in results) / total,
            4,
        ) if total else 0,
        "average_contacts": round(mean(item.contacts for item in results), 3) if total else 0,
        "average_resolution_day": round(mean(resolution_days), 3) if resolution_days else None,
        "confirmation_burden_per_episode": round(
            sum(item.confirmations for item in results) / total,
            3,
        ) if total else 0,
        "human_escalation_rate": round(
            sum(item.human_escalations > 0 for item in results) / total,
            4,
        ) if total else 0,
        "blocked_unsafe_actions": sum(item.blocked_unsafe_actions for item in results),
        "false_payment_state_changes": sum(item.false_payment_state_changes for item in results),
        "policy_violations": sum(item.policy_violations for item in results),
        "promises_created": sum(item.promises_created for item in results),
        "promises_broken": sum(item.promises_broken for item in results),
        "outcomes": dict(sorted(Counter(item.outcome for item in results).items())),
    }


def build_report(
    config: SimulatorConfig,
    results: list[EpisodeResult],
    episodes_per_persona: int,
) -> dict[str, Any]:
    by_policy = {
        policy.value: [item for item in results if item.policy == policy.value]
        for policy in SimulationPolicy
    }
    policies = {name: aggregate(items) for name, items in by_policy.items()}
    per_persona = {
        persona.id: {
            name: aggregate([item for item in items if item.persona_id == persona.id])
            for name, items in by_policy.items()
        }
        for persona in config.personas
    }
    baseline = policies[SimulationPolicy.GENERIC_EXACT_LINK.value]
    aware = policies[SimulationPolicy.PROMISE_AWARE_FIREWALL.value]
    traces = [
        {
            "policy": item.policy,
            "persona_id": item.persona_id,
            "episode": item.episode,
            "seed": item.seed,
            "outcome": item.outcome,
            "final_state": item.final_state,
            "events": list(item.trace),
        }
        for item in results
        if item.episode == 0
    ]
    core = {
        "report_type": "SYNTHETIC_SIMULATION_ONLY",
        "real_world_claim": False,
        "persona_set_version": config.version,
        "persona_sha256": config.sha256,
        "seed": config.seed,
        "persona_count": len(config.personas),
        "episodes_per_persona": episodes_per_persona,
        "total_episodes": len(results),
        "horizon_days": config.horizon_days,
        "claim_boundary": config.claim_boundary,
        "policies": policies,
        "per_persona": per_persona,
        "simulated_policy_delta": {
            "promise_aware_minus_baseline_amount_recovery_rate": round(
                aware["simulated_amount_recovery_rate"]
                - baseline["simulated_amount_recovery_rate"],
                4,
            ),
            "promise_aware_minus_baseline_average_contacts": round(
                aware["average_contacts"] - baseline["average_contacts"],
                3,
            ),
            "interpretation": (
                "A difference under the frozen fictional assumptions, not an "
                "estimate of real payment-recovery uplift."
            ),
        },
        "safety_gate": {
            "false_payment_state_changes_must_equal_zero": (
                aware["false_payment_state_changes"] == 0
                and baseline["false_payment_state_changes"] == 0
            ),
            "policy_violations_must_equal_zero": (
                aware["policy_violations"] == 0
                and baseline["policy_violations"] == 0
            ),
        },
        "example_traces": traces,
    }
    core["deterministic_result_sha256"] = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return core
