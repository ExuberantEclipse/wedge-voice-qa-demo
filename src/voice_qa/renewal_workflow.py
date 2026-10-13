"""Deterministic state for the inbound synthetic Medi-Cal renewal workflow."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping


class WorkflowError(RuntimeError):
    pass


@dataclass
class RenewalSession:
    call_id: str
    current_clinic: str = "Universal Community Health Center"
    last_consumed_caller_turn: str = ""
    renewal_status: str = "unknown"
    contact_preference: str = "unknown"
    facts: dict[str, str] = field(default_factory=dict)
    callback_request: dict[str, str] = field(default_factory=dict)
    updated_profile_fields: set[str] = field(default_factory=set)
    audit_events: list[Mapping[str, Any]] = field(default_factory=list)


class RenewalWorkflowController:
    RENEWAL_STATUSES = {
        "completed",
        "packet_received",
        "packet_not_received",
        "needs_help",
        "declined_help",
        "uncertain",
    }
    CONTACT_PREFERENCES = {
        "callback_requested",
        "conditional_callback",
        "no_callback",
        "callback_revoked",
        "do_not_call",
    }
    FACT_VALUES = {
        "medi_cal_status": {
            "active",
            "inactive",
            "changing",
            "uncertain",
            "declined",
        },
        "pcp_preference": {"keep_clinic", "change_clinic", "uncertain", "declined"},
        "moved_recently": {"yes", "no", "uncertain", "declined"},
        "contact_information_changed": {"yes", "no", "uncertain", "declined"},
        "packet_received": {"yes", "no", "uncertain"},
        "help_preference": {"phone", "clinic", "self_service", "none", "uncertain"},
        "language": {"english", "spanish", "other"},
    }

    def __init__(self):
        self._sessions: dict[str, RenewalSession] = {}
        self._lock = threading.RLock()

    def start(self, call_id: str) -> RenewalSession:
        with self._lock:
            return self._sessions.setdefault(call_id, RenewalSession(call_id=call_id))

    def session(self, call_id: str) -> RenewalSession:
        try:
            return self._sessions[call_id]
        except KeyError as error:
            raise WorkflowError("Unknown or expired renewal workflow") from error

    def record_renewal_status(self, call_id: str, status: str) -> RenewalSession:
        if status not in self.RENEWAL_STATUSES:
            raise WorkflowError("Unsupported renewal status")
        with self._lock:
            session = self.session(call_id)
            session.renewal_status = status
            # Compatibility for assistants created before packet answers were removed
            # from the renewal-status tool. Either valid tool must permanently close
            # the packet question.
            if status == "packet_received":
                session.facts["packet_received"] = "yes"
            elif status == "packet_not_received":
                session.facts["packet_received"] = "no"
            self._audit(session, "renewal_status", status)
            return session

    def record_fact(self, call_id: str, field_name: str, value: str) -> RenewalSession:
        allowed = self.FACT_VALUES.get(field_name)
        if not allowed or value not in allowed:
            raise WorkflowError("Unsupported workflow fact")
        with self._lock:
            session = self.session(call_id)
            session.facts[field_name] = value
            self._audit(session, "workflow_fact", "recorded", field=field_name, value=value)
            return session

    def set_current_clinic(self, call_id: str, clinic: str) -> RenewalSession:
        normalized = " ".join(clinic.split())[:120]
        if not normalized:
            raise WorkflowError("Current clinic must not be empty")
        with self._lock:
            session = self.session(call_id)
            session.current_clinic = normalized
            self._audit(session, "current_clinic", "loaded")
            return session

    def consume_caller_turn(self, call_id: str, fingerprint: str) -> RenewalSession:
        """Allow at most one state-changing tool call per distinct caller utterance."""
        if not fingerprint:
            return self.session(call_id)
        with self._lock:
            session = self.session(call_id)
            if session.last_consumed_caller_turn == fingerprint:
                raise WorkflowError("NO_NEW_CALLER_TURN")
            session.last_consumed_caller_turn = fingerprint
            self._audit(session, "caller_turn", "consumed")
            return session

    def next_step(self, call_id: str) -> Mapping[str, str]:
        """Return the single next demo action; the model does not choose the sequence."""
        with self._lock:
            session = self.session(call_id)
            if "declined" in session.facts.values() or session.renewal_status == "declined_help":
                return {
                    "step": "close_declined",
                    "say": "No problem. Thanks for your time, and take care.",
                }
            if session.renewal_status != "completed" and "medi_cal_status" not in session.facts:
                return {
                    "step": "coverage",
                    "say": (
                        "Is your Medi-Cal coverage still active, or are you planning "
                        "to change it?"
                    ),
                }
            if "pcp_preference" not in session.facts:
                return {
                    "step": "clinic",
                    "say": (
                        f"Is {session.current_clinic} still working well for you, or "
                        "would you like to change clinics?"
                    ),
                }
            if (
                session.facts.get("pcp_preference") == "change_clinic"
                and not {
                    "primary_care_clinic",
                    "primary_care_preference",
                }.intersection(session.updated_profile_fields)
            ):
                return {
                    "step": "collect_clinic_update",
                    "say": (
                        "Have you chosen a new clinic, or should I record that you want "
                        "to change clinics but have not selected one?"
                    ),
                }
            if session.renewal_status != "completed" and "packet_received" not in session.facts:
                return {
                    "step": "packet",
                    "say": "Have you received the yellow-envelope renewal packet?",
                }
            if session.renewal_status != "completed" and "help_preference" not in session.facts:
                return {
                    "step": "renewal_help",
                    "say": (
                        "Would you prefer free phone help, clinic help, or no help with "
                        "your renewal?"
                    ),
                }
            if session.facts.get("help_preference") == "uncertain":
                return {
                    "step": "renewal_help_clarification",
                    "say": (
                        "Phone help can guide you remotely, clinic help is in person, "
                        "and you can also continue on your own or choose no help. "
                        "Which option would you prefer?"
                    ),
                }
            if (
                session.facts.get("help_preference") == "phone"
                and session.contact_preference == "unknown"
            ):
                return {
                    "step": "callback_offer",
                    "say": (
                        "Phone help can walk through the renewal packet and next steps "
                        "with you. Would you like me to record a callback request?"
                    ),
                }
            if "contact_information_changed" not in session.facts:
                help_preference = session.facts.get("help_preference")
                guidance = {
                    "clinic": (
                        f"For in-person renewal help, contact {session.current_clinic}'s "
                        "enrollment desk and bring your renewal packet. "
                    ),
                    "self_service": (
                        "To continue on your own, use BenefitsCal.com or follow the "
                        "instructions in your renewal packet. "
                    ),
                    "none": "Okay, I won't add renewal help or ask about it again. ",
                }.get(help_preference, "")
                return {
                    "step": "contact_changes",
                    "say": guidance + (
                        "Before we finish, have you moved or changed your mailing "
                        "address, phone number, or other contact information?"
                    ),
                }
            if session.facts.get("contact_information_changed") == "yes":
                contact_fields = {
                    "mailing_address",
                    "contact_phone",
                    "preferred_language",
                    "secondary_language",
                }
                if contact_fields.intersection(session.updated_profile_fields):
                    return {
                        "step": "more_profile_updates",
                        "say": "Is there any other contact information you want to change?",
                    }
                return {
                    "step": "collect_profile_update",
                    "say": "Which information would you like to update first?",
                }
            return {
                "step": "complete",
                "say": (
                    "Thanks—that completes the renewal review. You're all done, "
                    "and you can hang up now. I'll end this call in about ten seconds."
                ),
            }

    def record_profile_update(self, call_id: str, field_name: str) -> RenewalSession:
        with self._lock:
            session = self.session(call_id)
            session.updated_profile_fields.add(field_name)
            self._audit(session, "profile_update", "recorded", field=field_name)
            return session

    def record_contact_preference(
        self,
        call_id: str,
        preference: str,
        caller_words: str,
        *,
        requested_time: str = "",
        condition: str = "",
        channel: str = "",
        purpose: str = "",
        timezone: str = "",
        language: str = "",
        agent_prompt: str = "",
    ) -> Mapping[str, Any]:
        if preference not in self.CONTACT_PREFERENCES:
            raise WorkflowError("Unsupported contact preference")
        with self._lock:
            session = self.session(call_id)
            if preference in {"no_callback", "callback_revoked", "do_not_call"}:
                session.contact_preference = preference
                session.callback_request = {}
                self._audit(session, "contact_preference", preference)
                return {"decision": "recorded", "preference": preference}

            if session.contact_preference in {
                "no_callback",
                "callback_revoked",
                "do_not_call",
            }:
                self._audit(session, "contact_preference", "blocked_by_refusal")
                return {
                    "decision": "blocked",
                    "reason": "A refusal or revocation is already active for this call.",
                }

            if _contains_callback_refusal(caller_words):
                session.contact_preference = "callback_revoked"
                session.callback_request = {}
                self._audit(session, "contact_preference", "callback_revoked")
                return {
                    "decision": "blocked",
                    "reason": "The supplied caller words contain a callback refusal.",
                }

            if not (
                _contains_explicit_callback_request(caller_words)
                or _contains_contextual_callback_consent(caller_words, agent_prompt)
            ):
                self._audit(session, "contact_preference", "missing_explicit_consent")
                return {
                    "decision": "blocked",
                    "reason": "Busy, later, or silence is not explicit callback consent.",
                }

            if preference == "conditional_callback" and not condition.strip():
                self._audit(session, "contact_preference", "missing_condition")
                return {
                    "decision": "blocked",
                    "reason": "Conditional consent must preserve the caller's condition.",
                }

            session.contact_preference = preference
            callback_request = {
                key: value.strip()
                for key, value in {
                    "requested_time": requested_time,
                    "condition": condition,
                    "channel": channel,
                    "purpose": purpose,
                    "timezone": timezone,
                    "language": language,
                }.items()
                if value.strip()
            }
            session.callback_request = callback_request
            self._audit(
                session,
                "contact_preference",
                preference,
                has_time=bool(requested_time.strip()),
                has_condition=bool(condition.strip()),
                channel=channel.strip().casefold()[:20],
                has_purpose=bool(purpose.strip()),
                has_timezone=bool(timezone.strip()),
                language=language.strip().casefold()[:20],
            )
            return {
                "decision": "recorded",
                "preference": preference,
                **callback_request,
            }

    def end(self, call_id: str) -> None:
        with self._lock:
            self._sessions.pop(call_id, None)

    @staticmethod
    def _audit(
        session: RenewalSession, event: str, decision: str, **metadata: Any
    ) -> None:
        session.audit_events.append(
            {"event": event, "decision": decision, "metadata": dict(metadata)}
        )


def _contains_callback_refusal(value: str) -> bool:
    return bool(
        re.search(
            r"\b(do not|don't|dont|no|never|stop|cancel|not)\b.{0,45}"
            r"\b(call|callback|calling|contact)\b|"
            r"\b(call|callback|calling|contact)\b.{0,45}"
            r"\b(do not|don't|dont|no|never|stop|cancel|not)\b|"
            r"\b(no me llame|no llame|no quiero que me llame|no vuelva a llamar|"
            r"deje de llamar|cancele la llamada)\b",
            value,
            flags=re.I,
        )
    )


def _contains_explicit_callback_request(value: str) -> bool:
    return bool(
        re.search(
            r"\b(call me|call us|give me a call|give us a call|"
            r"you can call|please call|callback|call back|"
            r"ll[aá]meme|puede llamarme|me puede llamar|vuelva a llamarme|"
            r"devu[eé]lvame la llamada)\b",
            value,
            flags=re.I,
        )
    )


def _contains_contextual_callback_consent(caller_words: str, agent_prompt: str) -> bool:
    affirmative = bool(
        re.fullmatch(
            r"\s*(yes|yeah|yep|sure|okay|ok|please|sounds good|"
            r"s[ií]|claro|est[aá] bien)\s*[.!]?\s*",
            caller_words,
            flags=re.I,
        )
    )
    direct_callback_question = bool(
        re.search(
            r"\b(callback|call you back|call me back|llamarle|devolverle la llamada)\b",
            agent_prompt,
            flags=re.I,
        )
    )
    return affirmative and direct_callback_question
