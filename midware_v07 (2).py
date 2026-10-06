"""
MIDWARE v0.7.0 — recursive, domain-extensible decision architecture
===================================================================

MIDWARE is the governed layer between fragmented facts and coordinated action.
Payments is the first worked example, not the boundary of the architecture.

Core loop
---------
sources -> canonical MidState -> candidate proposals -> policy -> one decision
        -> channel orchestration -> verified trace -> observed outcome --+
              ^----------------------------------------------------------+

Primary philosophy
------------------
1. Many source systems may speak; one canonical state is published.
2. AI, rules, or models may propose actions; they do not grant themselves
   authority to act.
3. Deterministic policy, review gates, consent, and verification surround the
   probabilistic layer.
4. Every channel receives the same decision identity.
5. Outcomes become experience that can change the next decision.
6. New domains join through modules instead of changing the core runtime.

Run this file directly:
    python3 midware_v07.py

Or import it:
    from midware_v07 import Midware, copy_sources

This is an executable reference architecture. It does not process cards,
connect to production systems, award credits, or certify compliance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol
import hashlib
import json
import math
import uuid


VERSION = "0.7.0"
SCHEMA_VERSION = "midware.state/0.7.0"
POLICY_VERSION = "midware.governance/0.7.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any, length: int = 16) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


# ============================================================
# 1. THE CANONICAL OBJECT: GOVERNED SIGNALS BECOME MIDSTATE
# ============================================================


@dataclass(frozen=True)
class SignalDefinition:
    name: str
    owner: str
    source_precedence: tuple[str, ...]
    required: bool = False
    max_age_minutes: int | None = None
    domain: str = "core"


@dataclass(frozen=True)
class Signal:
    name: str
    value: Any
    source: str
    observed_at: str
    quality: float = 1.0


@dataclass(frozen=True)
class SignalIssue:
    code: str
    signal: str
    severity: str
    detail: str
    domain: str = "core"


@dataclass(frozen=True)
class MidState:
    """The source-agnostic object every downstream component understands."""

    subject_id: str
    objective: str
    correlation_id: str
    assembled_at: str
    signals: tuple[Signal, ...]
    issues: tuple[SignalIssue, ...] = field(default_factory=tuple)
    schema_version: str = SCHEMA_VERSION

    def get(self, name: str, default: Any = None) -> Any:
        return next(
            (signal.value for signal in self.signals if signal.name == name),
            default,
        )

    def source_of(self, name: str) -> str | None:
        return next(
            (signal.source for signal in self.signals if signal.name == name),
            None,
        )

    @property
    def customer_id(self) -> str:
        """Compatibility name for the first customer/payments use case."""
        return self.subject_id

    @property
    def fingerprint(self) -> str:
        # Transport IDs and timestamps do not change semantic identity.
        payload = {
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "objective": self.objective,
            "signals": [
                {"name": item.name, "value": item.value}
                for item in sorted(self.signals, key=lambda item: item.name)
            ],
        }
        return stable_hash(payload)


# Compatibility for anyone who learned v0.6 using CustomerState.
CustomerState = MidState


class SignalFoundation:
    """
    Resolves disparate source payloads into one governed MidState.

    A source maps canonical names to a raw value or to:
        {"value": ..., "observed_at": ISO_TIME, "quality": 0.0_to_1.0}

    Domain validation is injected. The foundation itself knows how to govern
    signals, not what a payment, outage, churn, or fraud signal means.
    """

    PROHIBITED_FIELDS = {
        "pan",
        "card_number",
        "full_card_number",
        "cvv",
        "cvc",
        "track_data",
        "pin",
        "pin_block",
    }

    def __init__(
        self,
        catalog: Iterable[SignalDefinition],
        validators: Iterable[Any] = (),
    ) -> None:
        self.catalog = {item.name: item for item in catalog}
        self.validators = tuple(validators)

    def aggregate(
        self,
        subject_id: str,
        source_payloads: dict[str, dict[str, Any]],
        assembled_at: str | None = None,
        objective: str = "next_best_action",
    ) -> MidState:
        timestamp = assembled_at or utc_now()
        candidates: dict[str, list[Signal]] = defaultdict(list)
        issues: list[SignalIssue] = []

        for source, payload in source_payloads.items():
            for name, raw in payload.items():
                if name in self.PROHIBITED_FIELDS:
                    issues.append(SignalIssue(
                        "PROHIBITED_SENSITIVE_DATA",
                        name,
                        "fatal",
                        f"{source} attempted to publish protected card data",
                    ))
                    continue
                if name not in self.catalog:
                    issues.append(SignalIssue(
                        "UNKNOWN_SIGNAL",
                        name,
                        "warning",
                        f"{source} published a signal outside the active modules",
                    ))
                    continue
                value, observed_at, quality = self._unpack(raw, timestamp)
                candidates[name].append(
                    Signal(name, value, source, observed_at, quality)
                )

        published: list[Signal] = []
        for definition in self.catalog.values():
            choices = candidates.get(definition.name, [])
            if not choices:
                if definition.required:
                    issues.append(SignalIssue(
                        "MISSING_REQUIRED_SIGNAL",
                        definition.name,
                        "error",
                        f"required signal is missing; owner={definition.owner}",
                        definition.domain,
                    ))
                continue

            chosen = self._choose(definition, choices)
            published.append(chosen)
            distinct_values = {
                json.dumps(item.value, sort_keys=True, default=str)
                for item in choices
            }
            if len(distinct_values) > 1:
                issues.append(SignalIssue(
                    "SOURCE_CONFLICT",
                    definition.name,
                    "warning",
                    f"published {chosen.source} using catalog precedence",
                    definition.domain,
                ))
            freshness_issue = self._freshness_issue(
                definition,
                chosen,
                timestamp,
            )
            if freshness_issue:
                issues.append(freshness_issue)

        state = MidState(
            subject_id=subject_id,
            objective=objective,
            correlation_id=str(uuid.uuid4()),
            assembled_at=timestamp,
            signals=tuple(sorted(published, key=lambda item: item.name)),
            issues=tuple(issues),
        )

        domain_issues: list[SignalIssue] = []
        for validator in self.validators:
            domain_issues.extend(validator(state))
        if domain_issues:
            state = MidState(
                subject_id=state.subject_id,
                objective=state.objective,
                correlation_id=state.correlation_id,
                assembled_at=state.assembled_at,
                signals=state.signals,
                issues=state.issues + tuple(domain_issues),
                schema_version=state.schema_version,
            )
        return state

    @staticmethod
    def _unpack(raw: Any, default_time: str) -> tuple[Any, str, float]:
        if isinstance(raw, dict) and "value" in raw:
            return (
                raw["value"],
                str(raw.get("observed_at", default_time)),
                float(raw.get("quality", 1.0)),
            )
        return raw, default_time, 1.0

    @staticmethod
    def _choose(
        definition: SignalDefinition,
        choices: list[Signal],
    ) -> Signal:
        priority = {
            source: rank
            for rank, source in enumerate(definition.source_precedence)
        }
        return min(
            choices,
            key=lambda item: (
                priority.get(item.source, len(priority)),
                -item.quality,
                item.source,
            ),
        )

    @staticmethod
    def _freshness_issue(
        definition: SignalDefinition,
        signal: Signal,
        assembled_at: str,
    ) -> SignalIssue | None:
        if definition.max_age_minutes is None:
            return None
        try:
            observed = datetime.fromisoformat(signal.observed_at)
            assembled = datetime.fromisoformat(assembled_at)
            age_minutes = (assembled - observed).total_seconds() / 60
        except (TypeError, ValueError):
            return SignalIssue(
                "INVALID_TIMESTAMP",
                signal.name,
                "error",
                f"invalid observed_at={signal.observed_at!r}",
                definition.domain,
            )
        if age_minutes <= definition.max_age_minutes:
            return None
        return SignalIssue(
            "STALE_SIGNAL",
            signal.name,
            "warning",
            f"age={age_minutes:.1f}m; maximum={definition.max_age_minutes}m",
            definition.domain,
        )


# ============================================================
# 2. DOMAIN MODULES: EXTENSIONS AROUND A STABLE CORE
# ============================================================


@dataclass(frozen=True)
class ActionSpec:
    action: str
    domain: str
    message_key: str
    call_to_action: str
    priority_tier: int = 50
    requires_review: bool = False
    contact_cap_exempt: bool = False
    blocked_channels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ActionCandidate:
    action: str
    score: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PolicyContribution:
    allowed: bool = True
    review_required: bool = False
    guardrails: tuple[str, ...] = field(default_factory=tuple)
    suppressed_actions: tuple[str, ...] = field(default_factory=tuple)


class DomainModule:
    """
    Extension seam for payments, service, care, retention, fraud, and more.

    A module may add governed signals, registered actions, candidate logic,
    domain validation, and policy contributions. It cannot bypass the shared
    policy engine, orchestrator, verifier, or outcome loop.
    """

    name = "unnamed"
    signal_definitions: tuple[SignalDefinition, ...] = ()
    action_specs: tuple[ActionSpec, ...] = ()

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        return []

    def validate(self, state: MidState) -> list[SignalIssue]:
        return []

    def policy(self, state: MidState, action: str) -> PolicyContribution:
        return PolicyContribution()


def derive_dunning_group(days_past_due: int) -> str:
    """Illustrative bands; production boundaries belong in company policy."""
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "early"
    if days_past_due <= 60:
        return "middle"
    if days_past_due <= 90:
        return "late"
    return "severe"


class CustomerContextModule(DomainModule):
    name = "customer_context"
    signal_definitions = (
        SignalDefinition(
            "contacts_7d", "care", ("care",), False, 60, name
        ),
        SignalDefinition(
            "preferred_channel",
            "customer_profile",
            ("customer_profile",),
            False,
            1440,
            name,
        ),
        SignalDefinition(
            "sms_consent",
            "customer_profile",
            ("customer_profile",),
            False,
            1440,
            name,
        ),
        SignalDefinition(
            "email_consent",
            "customer_profile",
            ("customer_profile",),
            False,
            1440,
            name,
        ),
        SignalDefinition(
            "push_consent",
            "customer_profile",
            ("customer_profile",),
            False,
            1440,
            name,
        ),
    )


class PaymentModule(DomainModule):
    name = "payments"
    RETRYABLE_RESPONSES = {
        "processor_timeout",
        "temporary_unavailable",
        "network_error",
    }
    signal_definitions = (
        SignalDefinition(
            "balance_due", "billing", ("billing", "customer_profile"),
            True, 60, name,
        ),
        SignalDefinition(
            "days_past_due", "billing", ("billing", "collections"),
            True, 60, name,
        ),
        SignalDefinition(
            "dunning_group", "collections", ("collections", "billing"),
            False, 60, name,
        ),
        SignalDefinition(
            "payment_method_health", "payments", ("payments", "billing"),
            True, 15, name,
        ),
        SignalDefinition(
            "last_payment_response", "payments", ("payments",),
            False, 15, name,
        ),
        SignalDefinition(
            "failed_payment_count_30d", "payments", ("payments",),
            False, 60, name,
        ),
        SignalDefinition(
            "autopay_enabled", "payments", ("payments", "customer_profile"),
            False, 1440, name,
        ),
        SignalDefinition(
            "prior_delinquency_count", "collections", ("collections",),
            False, 1440, name,
        ),
        SignalDefinition(
            "payment_arrangement_active", "collections", ("collections",),
            False, 60, name,
        ),
    )
    action_specs = (
        ActionSpec(
            "payments.update_method", name,
            "payments.update_method.v1", "review_payment_method",
            priority_tier=70,
        ),
        ActionSpec(
            "payments.retry", name,
            "payments.retry.v1", "retry_now",
            priority_tier=60,
        ),
        ActionSpec(
            "payments.guided_recovery", name,
            "payments.guided_recovery.v1", "start_guided_payment",
            priority_tier=55,
        ),
        ActionSpec(
            "payments.offer_arrangement", name,
            "payments.offer_arrangement.v1", "view_payment_options",
            priority_tier=50,
        ),
        ActionSpec(
            "payments.explain_balance", name,
            "payments.explain_balance.v1", "review_balance",
            priority_tier=30,
        ),
    )

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        health = str(state.get("payment_method_health", "unknown"))
        response = str(state.get("last_payment_response", ""))
        past_due = int(state.get("days_past_due", 0))
        balance = float(state.get("balance_due", 0.0))
        failures = int(state.get("failed_payment_count_30d", 0))
        output: list[ActionCandidate] = []

        if health in {"expired", "invalid", "closed", "compromised"}:
            output.append(ActionCandidate(
                "payments.update_method",
                0.95,
                ("PAYMENT_METHOD_UNHEALTHY", f"METHOD_{health.upper()}"),
            ))
        if response in self.RETRYABLE_RESPONSES and health == "healthy":
            output.append(ActionCandidate(
                "payments.retry",
                0.89,
                ("RETRYABLE_RESPONSE", "PAYMENT_METHOD_HEALTHY"),
            ))
        if failures >= 2 and health == "healthy":
            output.append(ActionCandidate(
                "payments.guided_recovery",
                0.84,
                ("REPEATED_PAYMENT_FAILURE",),
            ))
        if (
            past_due >= 30
            and balance > 0
            and not state.get("payment_arrangement_active", False)
        ):
            output.append(ActionCandidate(
                "payments.offer_arrangement",
                0.82,
                (
                    "BALANCE_PAST_DUE",
                    f"DUNNING_{derive_dunning_group(past_due).upper()}",
                ),
            ))
        if balance > 0:
            output.append(ActionCandidate(
                "payments.explain_balance",
                0.62,
                ("OPEN_BALANCE",),
            ))
        return output

    def validate(self, state: MidState) -> list[SignalIssue]:
        published = state.get("dunning_group")
        if published is None:
            return []
        expected = derive_dunning_group(int(state.get("days_past_due", 0)))
        if str(published) == expected:
            return []
        return [SignalIssue(
            "DUNNING_STATE_MISMATCH",
            "dunning_group",
            "error",
            f"published={published!r}; derived={expected!r}",
            self.name,
        )]

    def policy(self, state: MidState, action: str) -> PolicyContribution:
        guardrails: list[str] = []
        suppressed: set[str] = set()
        if int(state.get("prior_delinquency_count", 0)) == 0:
            suppressed.add("collections.aggressive_treatment")
            guardrails.append("use lower-friction first-delinquency treatment")
        if state.get("payment_arrangement_active", False):
            suppressed.add("payments.offer_arrangement")
            guardrails.append("honor the active payment arrangement")
        return PolicyContribution(
            guardrails=tuple(guardrails),
            suppressed_actions=tuple(sorted(suppressed)),
        )


class ServiceRecoveryModule(DomainModule):
    name = "service"
    signal_definitions = (
        SignalDefinition(
            "recent_service_outage", "service", ("service",),
            True, 15, name,
        ),
        SignalDefinition(
            "technical_distress", "service", ("service", "care"),
            False, 15, name,
        ),
        SignalDefinition(
            "active_outage_ticket", "service", ("service",),
            False, 15, name,
        ),
        SignalDefinition(
            "outage_duration_hours", "service", ("service",),
            False, 15, name,
        ),
        SignalDefinition(
            "sla_breach", "service", ("service",),
            False, 15, name,
        ),
    )
    action_specs = (
        ActionSpec(
            "service.recovery", name,
            "service.recovery.v1", "view_service_support",
            priority_tier=90,
            contact_cap_exempt=True,
        ),
        ActionSpec(
            "service.evaluate_sla_credit_eligibility", name,
            "service.sla_credit_review.v1", "review_service_credit",
            priority_tier=95,
            requires_review=True,
            contact_cap_exempt=True,
        ),
    )

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        output: list[ActionCandidate] = []
        if (
            state.get("active_outage_ticket", False)
            and state.get("sla_breach", False)
            and float(state.get("outage_duration_hours", 0.0)) >= 4.0
        ):
            # This asks an entitlement system or reviewer to evaluate a credit.
            # It does not award money on its own.
            output.append(ActionCandidate(
                "service.evaluate_sla_credit_eligibility",
                0.985,
                ("SLA_BREACH_REVIEW", "COMPANY_CAUSED_FRICTION"),
            ))
        if state.get("recent_service_outage", False):
            output.append(ActionCandidate(
                "service.recovery",
                0.98,
                ("RECENT_SERVICE_OUTAGE", "COMPANY_CAUSED_FRICTION"),
            ))
        return output

    def validate(self, state: MidState) -> list[SignalIssue]:
        issues: list[SignalIssue] = []
        try:
            duration = float(state.get("outage_duration_hours", 0.0))
        except (TypeError, ValueError):
            issues.append(SignalIssue(
                "INVALID_SIGNAL_VALUE",
                "outage_duration_hours",
                "error",
                "outage duration must be numeric",
                self.name,
            ))
            return issues
        if duration < 0:
            issues.append(SignalIssue(
                "INVALID_SIGNAL_VALUE",
                "outage_duration_hours",
                "error",
                "outage duration cannot be negative",
                self.name,
            ))
        if state.get("sla_breach", False) and not state.get(
            "active_outage_ticket", False
        ):
            issues.append(SignalIssue(
                "SLA_STATE_MISMATCH",
                "sla_breach",
                "error",
                "SLA breach is true without an active outage ticket",
                self.name,
            ))
        return issues

    def policy(self, state: MidState, action: str) -> PolicyContribution:
        guardrails: list[str] = []
        suppressed: set[str] = set()
        if state.get("recent_service_outage", False):
            suppressed.update({
                "collections.disconnect_service",
                "collections.aggressive_treatment",
            })
            guardrails.append("apply service-recovery treatment after outage")
        if state.get("active_outage_ticket", False):
            suppressed.update({
                "collections.disconnect_service",
                "service.throttle",
            })
            guardrails.append(
                "never disconnect or throttle during an active outage ticket"
            )
        if state.get("technical_distress", False):
            suppressed.add("collections.aggressive_treatment")
            guardrails.append("acknowledge unresolved technical distress")
        return PolicyContribution(
            guardrails=tuple(guardrails),
            suppressed_actions=tuple(sorted(suppressed)),
        )


class CareModule(DomainModule):
    name = "care"
    signal_definitions = (
        SignalDefinition(
            "billing_dispute_open", "care", ("care", "billing"),
            False, 15, name,
        ),
    )
    action_specs = (
        ActionSpec(
            "care.billing_specialist_review", name,
            "care.billing_specialist_review.v1",
            "connect_to_billing_specialist",
            priority_tier=100,
            blocked_channels=("ivr",),
        ),
    )

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        if not state.get("billing_dispute_open", False):
            return []
        return [ActionCandidate(
            "care.billing_specialist_review",
            0.99,
            ("OPEN_BILLING_DISPUTE",),
        )]

    def policy(self, state: MidState, action: str) -> PolicyContribution:
        if not state.get("billing_dispute_open", False):
            return PolicyContribution()
        return PolicyContribution(
            review_required=True,
            guardrails=("do not intensify treatment during billing dispute",),
            suppressed_actions=(
                "collections.aggressive_treatment",
                "collections.disconnect_service",
            ),
        )


CORE_ACTIONS = (
    ActionSpec(
        "core.manual_review",
        "core",
        "core.manual_review.v1",
        "connect_to_agent",
        priority_tier=100,
        requires_review=True,
    ),
    ActionSpec(
        "core.no_action",
        "core",
        "core.no_action.v1",
        "none",
        priority_tier=0,
    ),
)


def default_modules() -> tuple[DomainModule, ...]:
    return (
        CustomerContextModule(),
        PaymentModule(),
        ServiceRecoveryModule(),
        CareModule(),
    )


class ModuleRegistry:
    """Composes modules while protecting canonical names from collisions."""

    def __init__(self, modules: Iterable[DomainModule]) -> None:
        self.modules = tuple(modules)
        names = [module.name for module in self.modules]
        if len(names) != len(set(names)):
            raise ValueError("module names must be unique")

        self.signal_catalog: dict[str, SignalDefinition] = {}
        self.action_specs: dict[str, ActionSpec] = {
            item.action: item for item in CORE_ACTIONS
        }
        for module in self.modules:
            for definition in module.signal_definitions:
                if definition.name in self.signal_catalog:
                    raise ValueError(
                        f"duplicate signal definition={definition.name!r}"
                    )
                self.signal_catalog[definition.name] = definition
            for spec in module.action_specs:
                if spec.action in self.action_specs:
                    raise ValueError(f"duplicate action={spec.action!r}")
                if spec.domain != module.name:
                    raise ValueError(
                        f"action {spec.action!r} must belong to {module.name!r}"
                    )
                if not spec.action.startswith(f"{module.name}."):
                    raise ValueError(
                        f"action {spec.action!r} must use its module namespace"
                    )
                self.action_specs[spec.action] = spec

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(module.name for module in self.modules)

    def validate(self, state: MidState) -> list[SignalIssue]:
        issues: list[SignalIssue] = []
        for module in self.modules:
            issues.extend(module.validate(state))
        return issues


# ============================================================
# 3. PROPOSALS, POLICY, AND THE RECURSIVE OUTCOME MEMORY
# ============================================================


class CandidateProvider(Protocol):
    """
    Intended AI insertion point.

    An implementation may use rules, ML, or an LLM. It may propose only
    registered actions; shared policy and verification retain final authority.
    """

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        ...


class ModuleCandidateProvider:
    def __init__(self, registry: ModuleRegistry) -> None:
        self.registry = registry

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        output: list[ActionCandidate] = []
        for module in self.registry.modules:
            output.extend(module.candidates(state))
        return output


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    review_required: bool
    guardrails: tuple[str, ...]
    suppressed_actions: tuple[str, ...]


class GovernancePolicy:
    """Deterministic authority around any candidate provider or domain."""

    REVIEW_ISSUES = {
        "MISSING_REQUIRED_SIGNAL",
        "DUNNING_STATE_MISMATCH",
        "INVALID_TIMESTAMP",
        "INVALID_SIGNAL_VALUE",
        "SLA_STATE_MISMATCH",
    }

    def __init__(self, registry: ModuleRegistry) -> None:
        self.registry = registry

    def evaluate(self, state: MidState, action: str) -> PolicyResult:
        spec = self.registry.action_specs.get(action)
        action_domain = spec.domain if spec else "core"
        relevant_issues = tuple(
            issue for issue in state.issues
            if issue.domain in {"core", action_domain}
        )
        fatal = any(issue.severity == "fatal" for issue in relevant_issues)
        review = fatal or any(
            issue.code in self.REVIEW_ISSUES for issue in relevant_issues
        )
        guardrails = [
            "never expose or persist raw payment credentials",
            "a proposal cannot bypass registered policy",
        ]
        suppressed: set[str] = set()
        module_allowed = True

        if spec is None:
            review = True
            module_allowed = False
            guardrails.append("reject unregistered action proposals")
        else:
            review = review or spec.requires_review

        for module in self.registry.modules:
            contribution = module.policy(state, action)
            module_allowed = module_allowed and contribution.allowed
            review = review or contribution.review_required
            guardrails.extend(contribution.guardrails)
            suppressed.update(contribution.suppressed_actions)

        # Manual review is the fail-safe destination, including after fatal
        # signal issues. It is not an automated customer action.
        if action == "core.manual_review":
            allowed = True
            review = True
        else:
            allowed = module_allowed and not fatal and action not in suppressed

        return PolicyResult(
            allowed=allowed,
            review_required=review,
            guardrails=ordered_unique(guardrails),
            suppressed_actions=tuple(sorted(suppressed)),
        )


@dataclass(frozen=True)
class DecisionPlan:
    action: str
    domain: str
    priority_tier: int
    reason_codes: tuple[str, ...]
    guardrails: tuple[str, ...]
    policy_version: str = POLICY_VERSION

    def canonical(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "domain": self.domain,
            "priority_tier": self.priority_tier,
            "reason_codes": list(self.reason_codes),
            "guardrails": list(self.guardrails),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class Decision:
    decision_id: str
    subject_id: str
    state_fingerprint: str
    action: str
    domain: str
    priority_tier: int
    confidence: float
    reason_codes: tuple[str, ...]
    alternatives: tuple[str, ...]
    guardrails: tuple[str, ...]
    suppressed_actions: tuple[str, ...]
    review_required: bool
    experience_count: int
    learned_reliability: float
    plan: DecisionPlan

    @property
    def customer_id(self) -> str:
        return self.subject_id


@dataclass(frozen=True)
class OutcomeRecord:
    outcome_id: str
    decision_id: str
    action: str
    context_key: str
    state_fingerprint: str
    resolved: bool
    recorded_at: str
    details: tuple[tuple[str, Any], ...] = field(default_factory=tuple)


class OutcomeLedger:
    """
    Auditable experience memory with a small Bayesian prior.

    Reliability is learned first for the same action/reason context and then,
    when that context is new, from the action's broader history. This lets
    outcomes influence the next cycle without pretending that history is truth.
    """

    def __init__(self) -> None:
        self._records: list[OutcomeRecord] = []

    @staticmethod
    def context_key(reason_codes: Iterable[str]) -> str:
        return stable_hash(sorted(reason_codes), 12)

    def record(
        self,
        decision: Decision,
        resolved: bool,
        details: dict[str, Any] | None = None,
    ) -> OutcomeRecord:
        record = OutcomeRecord(
            outcome_id=f"out_{uuid.uuid4().hex}",
            decision_id=decision.decision_id,
            action=decision.action,
            context_key=self.context_key(decision.reason_codes),
            state_fingerprint=decision.state_fingerprint,
            resolved=bool(resolved),
            recorded_at=utc_now(),
            details=tuple(sorted((details or {}).items())),
        )
        self._records.append(record)
        return record

    def _matching(
        self,
        action: str,
        reason_codes: Iterable[str],
    ) -> list[OutcomeRecord]:
        context = self.context_key(reason_codes)
        exact = [
            item for item in self._records
            if item.action == action and item.context_key == context
        ]
        if exact:
            return exact
        return [item for item in self._records if item.action == action]

    def reliability(
        self,
        action: str,
        reason_codes: Iterable[str] = (),
    ) -> float:
        records = self._matching(action, reason_codes)
        return (sum(item.resolved for item in records) + 2.0) / (
            len(records) + 4.0
        )

    def count(
        self,
        action: str,
        reason_codes: Iterable[str] = (),
    ) -> int:
        return len(self._matching(action, reason_codes))

    @property
    def records(self) -> tuple[OutcomeRecord, ...]:
        return tuple(self._records)


class DecisionEngine:
    EXPERIENCE_WEIGHT = 0.15

    def __init__(
        self,
        registry: ModuleRegistry,
        provider: CandidateProvider,
        policy: GovernancePolicy,
        outcomes: OutcomeLedger,
    ) -> None:
        self.registry = registry
        self.provider = provider
        self.policy = policy
        self.outcomes = outcomes

    def _rank_value(self, candidate: ActionCandidate) -> float:
        learned = self.outcomes.reliability(
            candidate.action,
            candidate.reason_codes,
        )
        # Center the prior at zero so unseen actions receive no artificial lift.
        return candidate.score + self.EXPERIENCE_WEIGHT * (learned - 0.5)

    def _priority(self, candidate: ActionCandidate) -> int:
        spec = self.registry.action_specs.get(candidate.action)
        return spec.priority_tier if spec else -1

    def decide(self, state: MidState) -> Decision:
        proposed = list(self.provider.candidates(state))
        valid_scores = [
            item for item in proposed
            if math.isfinite(item.score) and 0.0 <= item.score <= 1.0
        ]
        ranked = sorted(
            valid_scores,
            key=lambda item: (
                -self._priority(item),
                -self._rank_value(item),
                item.action,
            ),
        )

        selected: ActionCandidate | None = None
        policy_result: PolicyResult | None = None
        for candidate in ranked:
            evaluation = self.policy.evaluate(state, candidate.action)
            if evaluation.allowed:
                selected, policy_result = candidate, evaluation
                break

        if selected is None:
            has_blocking_issue = any(
                issue.severity in {"error", "fatal"}
                for issue in state.issues
            )
            fallback_action = (
                "core.manual_review"
                if proposed or has_blocking_issue else "core.no_action"
            )
            fallback_reason = (
                "NO_SAFE_REGISTERED_ACTION"
                if proposed or has_blocking_issue else "NO_ACTION_NEEDED"
            )
            selected = ActionCandidate(
                fallback_action,
                1.0 if proposed else 0.80,
                (fallback_reason,),
            )
            policy_result = self.policy.evaluate(state, selected.action)

        assert policy_result is not None
        spec = self.registry.action_specs[selected.action]
        learned = self.outcomes.reliability(
            selected.action,
            selected.reason_codes,
        )
        experience_count = self.outcomes.count(
            selected.action,
            selected.reason_codes,
        )
        relevant_issues = tuple(
            issue for issue in state.issues
            if issue.domain in {"core", spec.domain}
        )
        issue_penalty = min(
            0.40,
            0.08 * sum(
                issue.severity in {"warning", "error"}
                for issue in relevant_issues
            ),
        )
        confidence = round(
            max(
                0.05,
                min(0.99, 0.80 * selected.score + 0.20 * learned - issue_penalty),
            ),
            3,
        )
        plan = DecisionPlan(
            action=selected.action,
            domain=spec.domain,
            priority_tier=spec.priority_tier,
            reason_codes=selected.reason_codes,
            guardrails=policy_result.guardrails,
        )
        decision_key = {
            "subject_id": state.subject_id,
            "state": state.fingerprint,
            "plan": plan.canonical(),
        }
        alternatives = ordered_unique(
            candidate.action
            for candidate in ranked
            if candidate.action != selected.action
        )
        return Decision(
            decision_id=f"dec_{stable_hash(decision_key, 20)}",
            subject_id=state.subject_id,
            state_fingerprint=state.fingerprint,
            action=selected.action,
            domain=spec.domain,
            priority_tier=spec.priority_tier,
            confidence=confidence,
            reason_codes=selected.reason_codes,
            alternatives=alternatives,
            guardrails=policy_result.guardrails,
            suppressed_actions=policy_result.suppressed_actions,
            review_required=policy_result.review_required,
            experience_count=experience_count,
            learned_reliability=round(learned, 3),
            plan=plan,
        )


# ============================================================
# 4. ONE DECISION, MANY CHANNELS — WITH A REAL REVIEW GATE
# ============================================================


@dataclass(frozen=True)
class ChannelDefinition:
    name: str
    kind: str
    consent_signal: str | None = None


DEFAULT_CHANNELS = (
    ChannelDefinition("agent_desktop", "human_assisted"),
    ChannelDefinition("app", "inbound"),
    ChannelDefinition("web", "inbound"),
    ChannelDefinition("sms", "proactive_outbound", "sms_consent"),
    ChannelDefinition("email", "proactive_outbound", "email_consent"),
    ChannelDefinition("push", "proactive_outbound", "push_consent"),
    ChannelDefinition("ivr", "proactive_outbound"),
)


@dataclass(frozen=True)
class ChannelInstruction:
    channel: str
    kind: str
    decision_id: str
    action: str
    message_key: str
    call_to_action: str
    enabled: bool
    preferred: bool
    suppression_reason: str | None = None


@dataclass(frozen=True)
class OrchestratedDecision:
    decision: Decision
    channels: tuple[ChannelInstruction, ...]


class ChannelOrchestrator:
    """
    Publishes one decision consistently while respecting delivery controls.

    The v0.7 correction: review_required is now an enforced control, not merely
    audit metadata. Automated channels are held; a human review path remains.
    """

    CONTACT_CAP_7D = 3

    def __init__(
        self,
        registry: ModuleRegistry,
        channels: Iterable[ChannelDefinition] = DEFAULT_CHANNELS,
    ) -> None:
        self.registry = registry
        self.channels = tuple(channels)

    def orchestrate(
        self,
        decision: Decision,
        state: MidState,
    ) -> OrchestratedDecision:
        spec = self.registry.action_specs[decision.action]
        preferred_channel = str(state.get("preferred_channel", ""))
        instructions: list[ChannelInstruction] = []
        for channel in self.channels:
            enabled, reason = self._availability(
                channel,
                state,
                decision,
                spec,
            )
            instructions.append(ChannelInstruction(
                channel=channel.name,
                kind=channel.kind,
                decision_id=decision.decision_id,
                action=decision.action,
                message_key=spec.message_key,
                call_to_action=spec.call_to_action,
                enabled=enabled,
                preferred=enabled and channel.name == preferred_channel,
                suppression_reason=reason,
            ))
        return OrchestratedDecision(decision, tuple(instructions))

    def _availability(
        self,
        channel: ChannelDefinition,
        state: MidState,
        decision: Decision,
        spec: ActionSpec,
    ) -> tuple[bool, str | None]:
        # Highest precedence: customer automation cannot leak an unapproved
        # decision. Human-assisted review remains available and visible.
        if decision.review_required and channel.kind != "human_assisted":
            return False, "held pending human review"

        if channel.name in spec.blocked_channels:
            return False, "action requires a richer channel"

        if channel.consent_signal and not state.get(
            channel.consent_signal,
            False,
        ):
            return False, (
                f"{channel.consent_signal} is false or unavailable"
            )

        # Contact pressure applies only to proactive outreach. It must never
        # lock a customer out of an app, website, or human support channel.
        if (
            channel.kind == "proactive_outbound"
            and int(state.get("contacts_7d", 0)) >= self.CONTACT_CAP_7D
            and not spec.contact_cap_exempt
        ):
            return False, (
                f"proactive_contact_capped: {state.get('contacts_7d')} "
                f"contacts in 7d (cap={self.CONTACT_CAP_7D})"
            )
        return True, None


class DecisionVerifier:
    """Independently verifies state, decision, control, and channel invariants."""

    def __init__(self, registry: ModuleRegistry) -> None:
        self.registry = registry

    def verify(
        self,
        state: MidState,
        package: OrchestratedDecision,
    ) -> dict[str, Any]:
        decision = package.decision
        expected_id = "dec_" + stable_hash({
            "subject_id": state.subject_id,
            "state": state.fingerprint,
            "plan": decision.plan.canonical(),
        }, 20)
        review_channels_gated = (
            all(
                not item.enabled
                for item in package.channels
                if item.kind != "human_assisted"
            )
            if decision.review_required else True
        )
        reviewer_path_available = (
            any(
                item.enabled and item.kind == "human_assisted"
                for item in package.channels
            )
            if decision.review_required else True
        )
        checks = {
            "state_identity_preserved": (
                decision.state_fingerprint == state.fingerprint
            ),
            "decision_id_reproducible": decision.decision_id == expected_id,
            "action_is_registered": (
                decision.action in self.registry.action_specs
            ),
            "channels_reference_one_decision": all(
                item.decision_id == decision.decision_id
                for item in package.channels
            ),
            "channels_reference_one_action": all(
                item.action == decision.action
                for item in package.channels
            ),
            "suppressed_action_not_selected": (
                decision.action not in decision.suppressed_actions
            ),
            "no_sensitive_signal_published": all(
                signal.name not in SignalFoundation.PROHIBITED_FIELDS
                for signal in state.signals
            ),
            "review_gate_enforced": review_channels_gated,
            "reviewer_path_available": reviewer_path_available,
        }
        return {"passed": all(checks.values()), "checks": checks}


# ============================================================
# 5. RUNTIME, AUDIT TRACE, AND RECURSIVE FEEDBACK
# ============================================================


@dataclass(frozen=True)
class DecisionTrace:
    trace_id: str
    state: MidState
    package: OrchestratedDecision
    verification: dict[str, Any]
    modules: tuple[str, ...]
    generated_at: str

    def audit_summary(self) -> dict[str, Any]:
        decision = self.package.decision
        return {
            "runtime_version": VERSION,
            "trace_id": self.trace_id,
            "correlation_id": self.state.correlation_id,
            "subject_id": self.state.subject_id,
            "objective": self.state.objective,
            "state_fingerprint": self.state.fingerprint,
            "active_modules": self.modules,
            "sources": {
                signal.name: signal.source for signal in self.state.signals
            },
            "signal_issues": [
                {
                    "code": issue.code,
                    "signal": issue.signal,
                    "severity": issue.severity,
                    "detail": issue.detail,
                    "domain": issue.domain,
                }
                for issue in self.state.issues
            ],
            "decision": {
                "decision_id": decision.decision_id,
                "domain": decision.domain,
                "action": decision.action,
                "priority_tier": decision.priority_tier,
                "confidence": decision.confidence,
                "reason_codes": decision.reason_codes,
                "alternatives": decision.alternatives,
                "guardrails": decision.guardrails,
                "suppressed_actions": decision.suppressed_actions,
                "review_required": decision.review_required,
                "experience_count": decision.experience_count,
                "learned_reliability": decision.learned_reliability,
            },
            "channels": [
                {
                    "channel": item.channel,
                    "kind": item.kind,
                    "enabled": item.enabled,
                    "preferred": item.preferred,
                    "message_key": item.message_key,
                    "call_to_action": item.call_to_action,
                    "suppression_reason": item.suppression_reason,
                }
                for item in self.package.channels
            ],
            "verification": self.verification,
            "generated_at": self.generated_at,
        }


class Midware:
    """Facade for canonical state, governed decisions, channels, and learning."""

    def __init__(
        self,
        modules: Iterable[DomainModule] | None = None,
        extra_modules: Iterable[DomainModule] = (),
        provider: CandidateProvider | None = None,
    ) -> None:
        selected_modules = tuple(modules) if modules is not None else default_modules()
        selected_modules = selected_modules + tuple(extra_modules)
        self.registry = ModuleRegistry(selected_modules)
        self.foundation = SignalFoundation(
            self.registry.signal_catalog.values(),
            validators=(self.registry.validate,),
        )
        self.outcomes = OutcomeLedger()
        self.provider = provider or ModuleCandidateProvider(self.registry)
        self.policy = GovernancePolicy(self.registry)
        self.engine = DecisionEngine(
            registry=self.registry,
            provider=self.provider,
            policy=self.policy,
            outcomes=self.outcomes,
        )
        self.orchestrator = ChannelOrchestrator(self.registry)
        self.verifier = DecisionVerifier(self.registry)
        self._decisions: dict[str, Decision] = {}

    def decide(
        self,
        customer_id: str,
        source_payloads: dict[str, dict[str, Any]],
        assembled_at: str | None = None,
        objective: str = "next_best_action",
    ) -> DecisionTrace:
        # customer_id remains the friendly API name; internally it is the more
        # general subject_id so the same runtime can decide about other entities.
        state = self.foundation.aggregate(
            subject_id=customer_id,
            source_payloads=source_payloads,
            assembled_at=assembled_at,
            objective=objective,
        )
        decision = self.engine.decide(state)
        package = self.orchestrator.orchestrate(decision, state)
        verification = self.verifier.verify(state, package)
        self._decisions[decision.decision_id] = decision
        return DecisionTrace(
            trace_id=str(uuid.uuid4()),
            state=state,
            package=package,
            verification=verification,
            modules=self.registry.module_names,
            generated_at=utc_now(),
        )

    def record_outcome(
        self,
        decision_id: str,
        resolved: bool,
        details: dict[str, Any] | None = None,
    ) -> OutcomeRecord:
        try:
            decision = self._decisions[decision_id]
        except KeyError as exc:
            raise KeyError(f"unknown decision_id={decision_id}") from exc
        return self.outcomes.record(decision, resolved, details)


# ============================================================
# 6. SAMPLE DATA AND EXECUTABLE SPECIFICATION
# ============================================================


SAMPLE_SOURCES: dict[str, dict[str, Any]] = {
    "billing": {
        "balance_due": 164.25,
        "days_past_due": 34,
    },
    "collections": {
        "dunning_group": "middle",
        "prior_delinquency_count": 0,
        "payment_arrangement_active": False,
    },
    "payments": {
        "payment_method_health": "expired",
        "last_payment_response": "expired_card",
        "failed_payment_count_30d": 1,
        "autopay_enabled": True,
    },
    "service": {
        "recent_service_outage": True,
        "technical_distress": False,
        "active_outage_ticket": False,
        "outage_duration_hours": 0,
        "sla_breach": False,
    },
    "care": {
        "billing_dispute_open": False,
        "contacts_7d": 2,
    },
    "customer_profile": {
        "preferred_channel": "app",
        "sms_consent": False,
        "email_consent": True,
        "push_consent": True,
    },
}


def copy_sources() -> dict[str, dict[str, Any]]:
    return {
        source: dict(payload)
        for source, payload in SAMPLE_SOURCES.items()
    }


class RetentionExampleModule(DomainModule):
    """Tiny proof that a non-payment domain can join without core edits."""

    name = "retention"
    signal_definitions = (
        SignalDefinition(
            "churn_risk", "retention", ("crm",), True, 60, name
        ),
    )
    action_specs = (
        ActionSpec(
            "retention.review_save_offer", name,
            "retention.save_offer.v1", "review_retention_options",
            priority_tier=40,
        ),
    )

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        if float(state.get("churn_risk", 0.0)) < 0.80:
            return []
        return [ActionCandidate(
            "retention.review_save_offer",
            0.91,
            ("HIGH_CHURN_RISK",),
        )]


class LearningExampleModule(DomainModule):
    """Test-only pair of close choices that makes feedback visible."""

    name = "learning_example"
    signal_definitions = (
        SignalDefinition(
            "case_ready", "lab", ("lab",), True, 60, name
        ),
    )
    action_specs = (
        ActionSpec(
            "learning_example.option_a", name,
            "learning.option_a.v1", "choose_a",
        ),
        ActionSpec(
            "learning_example.option_b", name,
            "learning.option_b.v1", "choose_b",
        ),
    )

    def candidates(self, state: MidState) -> list[ActionCandidate]:
        if not state.get("case_ready", False):
            return []
        return [
            ActionCandidate(
                "learning_example.option_a", 0.82, ("SAME_CONTEXT",)
            ),
            ActionCandidate(
                "learning_example.option_b", 0.80, ("SAME_CONTEXT",)
            ),
        ]


def self_test() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    base = Midware()
    trace = base.decide(
        "customer-001",
        copy_sources(),
        "2026-09-10T12:00:00+00:00",
    )
    decision = trace.package.decision

    tests.append({
        "test": "service context changes a payment journey",
        "passed": (
            decision.action == "service.recovery"
            and "collections.aggressive_treatment"
            in decision.suppressed_actions
            and "collections.disconnect_service"
            in decision.suppressed_actions
        ),
    })
    tests.append({
        "test": "decision package independently verifies",
        "passed": trace.verification["passed"],
    })
    tests.append({
        "test": "every channel references one canonical decision",
        "passed": all(
            item.decision_id == decision.decision_id
            and item.action == decision.action
            for item in trace.package.channels
        ),
    })
    sms = next(item for item in trace.package.channels if item.channel == "sms")
    tests.append({
        "test": "channel consent is enforced",
        "passed": not sms.enabled and sms.suppression_reason is not None,
    })

    no_outage = copy_sources()
    no_outage["service"]["recent_service_outage"] = False
    ordinary = Midware().decide(
        "customer-002",
        no_outage,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "expired method receives a focused payment action",
        "passed": ordinary.package.decision.action == "payments.update_method",
    })

    conflict = copy_sources()
    conflict["customer_profile"]["balance_due"] = 999.00
    conflict_state = base.foundation.aggregate(
        "customer-003",
        conflict,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "source precedence publishes one accountable value",
        "passed": (
            conflict_state.get("balance_due") == 164.25
            and conflict_state.source_of("balance_due") == "billing"
            and any(
                issue.code == "SOURCE_CONFLICT"
                for issue in conflict_state.issues
            )
        ),
    })

    wrong_dunning = copy_sources()
    wrong_dunning["collections"]["dunning_group"] = "early"
    wrong_dunning["service"]["recent_service_outage"] = False
    dunning_trace = Midware().decide(
        "customer-004",
        wrong_dunning,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "review_required is an enforced gate, not metadata",
        "passed": (
            dunning_trace.package.decision.review_required
            and all(
                not item.enabled
                for item in dunning_trace.package.channels
                if item.kind != "human_assisted"
            )
            and any(
                item.enabled and item.kind == "human_assisted"
                for item in dunning_trace.package.channels
            )
            and dunning_trace.verification["checks"]["review_gate_enforced"]
        ),
    })

    sensitive = copy_sources()
    sensitive["payments"]["card_number"] = "4111111111111111"
    sensitive_trace = Midware().decide(
        "customer-005",
        sensitive,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "raw credentials are rejected and automation stops",
        "passed": (
            sensitive_trace.state.get("card_number") is None
            and sensitive_trace.package.decision.action == "core.manual_review"
            and sensitive_trace.verification["passed"]
        ),
    })

    repeated = base.decide(
        "customer-001",
        copy_sources(),
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "equivalent state produces an idempotent decision ID",
        "passed": repeated.package.decision.decision_id == decision.decision_id,
    })

    stale = copy_sources()
    stale["payments"]["payment_method_health"] = {
        "value": "expired",
        "observed_at": "2026-09-10T10:00:00+00:00",
    }
    stale_state = base.foundation.aggregate(
        "customer-006",
        stale,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "stale decision signals remain visible",
        "passed": any(
            issue.code == "STALE_SIGNAL"
            and issue.signal == "payment_method_health"
            for issue in stale_state.issues
        ),
    })

    capped = copy_sources()
    capped["service"]["recent_service_outage"] = False
    capped["care"]["contacts_7d"] = 5
    capped["customer_profile"].update({
        "sms_consent": True,
        "email_consent": True,
        "push_consent": True,
    })
    capped_trace = Midware().decide(
        "customer-007",
        capped,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "contact cap blocks outreach but not inbound help",
        "passed": (
            all(
                not item.enabled
                for item in capped_trace.package.channels
                if item.kind == "proactive_outbound"
            )
            and all(
                item.enabled
                for item in capped_trace.package.channels
                if item.kind in {"inbound", "human_assisted"}
            )
        ),
    })

    sla = copy_sources()
    sla["service"].update({
        "active_outage_ticket": True,
        "sla_breach": True,
        "outage_duration_hours": 6,
    })
    sla_trace = Midware().decide(
        "customer-008",
        sla,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "SLA signal requests eligibility review, never auto-awards money",
        "passed": (
            sla_trace.package.decision.action
            == "service.evaluate_sla_credit_eligibility"
            and sla_trace.package.decision.review_required
            and all(
                not item.enabled
                for item in sla_trace.package.channels
                if item.kind != "human_assisted"
            )
        ),
    })

    service_pressure = copy_sources()
    service_pressure["care"]["contacts_7d"] = 6
    service_pressure["customer_profile"].update({
        "sms_consent": True,
        "email_consent": True,
        "push_consent": True,
    })
    service_trace = Midware().decide(
        "customer-009",
        service_pressure,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "company-caused recovery may bypass routine contact pressure",
        "passed": any(
            item.enabled
            for item in service_trace.package.channels
            if item.kind == "proactive_outbound"
        ),
    })

    retention = Midware(modules=(
        CustomerContextModule(),
        RetentionExampleModule(),
    ))
    retention_trace = retention.decide(
        "customer-010",
        {
            "crm": {"churn_risk": 0.93},
            "customer_profile": {
                "preferred_channel": "web",
                "sms_consent": False,
                "email_consent": True,
                "push_consent": False,
            },
        },
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "non-payment module plugs in without core changes",
        "passed": (
            retention_trace.package.decision.action
            == "retention.review_save_offer"
            and retention_trace.package.decision.domain == "retention"
            and retention_trace.verification["passed"]
        ),
    })

    scoped_runtime = Midware(extra_modules=(RetentionExampleModule(),))
    scoped_trace = scoped_runtime.decide(
        "customer-011",
        copy_sources(),
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "one domain's missing signal does not freeze another domain",
        "passed": (
            scoped_trace.package.decision.action == "service.recovery"
            and not scoped_trace.package.decision.review_required
            and any(
                issue.code == "MISSING_REQUIRED_SIGNAL"
                and issue.domain == "retention"
                for issue in scoped_trace.state.issues
            )
        ),
    })

    learner = Midware(modules=(LearningExampleModule(),))
    learning_sources = {"lab": {"case_ready": True}}
    first = learner.decide(
        "case-001",
        learning_sources,
        "2026-09-10T12:00:00+00:00",
    )
    for _ in range(4):
        learner.record_outcome(
            first.package.decision.decision_id,
            resolved=False,
            details={"result": "did_not_resolve"},
        )
    next_cycle = learner.decide(
        "case-001",
        learning_sources,
        "2026-09-10T12:00:00+00:00",
    )
    tests.append({
        "test": "observed outcomes can change the next decision cycle",
        "passed": (
            first.package.decision.action == "learning_example.option_a"
            and next_cycle.package.decision.action
            == "learning_example.option_b"
            and len(learner.outcomes.records) == 4
        ),
    })

    return {
        "version": VERSION,
        "tests": tests,
        "passed": sum(test["passed"] for test in tests),
        "total": len(tests),
        "all_passed": all(test["passed"] for test in tests),
    }


if __name__ == "__main__":
    report = self_test()
    print("\nMIDWARE v0.7.0 TESTS\n" + "=" * 52)
    for item in report["tests"]:
        mark = "PASS" if item["passed"] else "FAIL"
        print(f"[{mark}] {item['test']}")
    print(
        f"\nOVERALL: {'PASS' if report['all_passed'] else 'FAIL'} "
        f"({report['passed']}/{report['total']})"
    )

    demo = Midware().decide(
        "customer-001",
        copy_sources(),
        "2026-09-10T12:00:00+00:00",
    )
    print("\nRECURSIVE DECISION TRACE\n" + "=" * 52)
    print(json.dumps(demo.audit_summary(), indent=2, default=str))
