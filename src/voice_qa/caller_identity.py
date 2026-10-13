"""Provider-independent caller-number lookup and deterministic verification gate."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import is_e164, normalize_phone


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoMember:
    id: str
    display_name: str
    phone_hmacs: tuple[str, ...]
    pin_hmac: str
    safe_context: Mapping[str, str]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DemoMember":
        return cls(
            id=str(value["id"]),
            display_name=str(value["display_name"]),
            phone_hmacs=tuple(str(item) for item in value.get("phone_hmacs") or []),
            pin_hmac=str(value["pin_hmac"]),
            safe_context={
                str(key): str(item)
                for key, item in dict(value.get("safe_context") or {}).items()
            },
        )

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "phone_hmacs": list(self.phone_hmacs),
            "pin_hmac": self.pin_hmac,
            "safe_context": dict(self.safe_context),
        }


class DemoDirectory:
    def __init__(
        self,
        pepper: str,
        members: Sequence[DemoMember],
        *,
        path: Path | None = None,
    ):
        if len(pepper) < 32:
            raise IdentityError("TWILIO_DEMO_PEPPER must contain at least 32 characters")
        self._pepper = pepper
        self._path = path
        self._lock = threading.RLock()
        self._members = {item.id: item for item in members}
        self._by_phone: dict[str, list[str]] = {}
        for member in members:
            for token in member.phone_hmacs:
                self._by_phone.setdefault(token, []).append(member.id)

    @classmethod
    def from_file(cls, path: Path, pepper: str) -> "DemoDirectory":
        if not path.exists():
            raise IdentityError(
                f"Demo directory does not exist: {path}. Run voice-qa-twilio-demo seed first."
            )
        value = json.loads(path.read_text(encoding="utf-8"))
        rows = value.get("members") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise IdentityError(f"{path}: expected a members array")
        return cls(pepper, [DemoMember.from_mapping(item) for item in rows], path=path)

    @property
    def members(self) -> Sequence[DemoMember]:
        return tuple(self._members.values())

    def member(self, member_id: str) -> DemoMember:
        try:
            return self._members[member_id]
        except KeyError as error:
            raise IdentityError("Unknown synthetic member") from error

    def candidates(self, caller_number: str) -> tuple[str, ...]:
        normalized = normalize_phone(caller_number)
        if not is_e164(normalized):
            return ()
        return tuple(self._by_phone.get(phone_hmac(self._pepper, normalized), ()))

    def claim_name(
        self, claimed_name: str, candidate_ids: Sequence[str] | None = None
    ) -> str | None:
        pool = candidate_ids or tuple(self._members)
        normalized = _normalize_name(claimed_name)
        matches = [
            member_id
            for member_id in pool
            if _normalize_name(self.member(member_id).display_name) == normalized
        ]
        return matches[0] if len(matches) == 1 else None

    def verify_pin(self, member_id: str, pin: str) -> bool:
        expected = self.member(member_id).pin_hmac
        actual = pin_hmac(self._pepper, member_id, pin)
        return hmac.compare_digest(expected, actual)

    def update_safe_context(
        self, member_id: str, updates: Mapping[str, str]
    ) -> DemoMember:
        with self._lock:
            if self._path is None:
                raise IdentityError("Synthetic member directory is not persistent")
            existing = self.member(member_id)
            updated = DemoMember(
                id=existing.id,
                display_name=existing.display_name,
                phone_hmacs=existing.phone_hmacs,
                pin_hmac=existing.pin_hmac,
                safe_context={**dict(existing.safe_context), **dict(updates)},
            )
            self._members[member_id] = updated
            self._persist()
            return updated

    def _persist(self) -> None:
        if self._path is None:
            raise IdentityError("Synthetic member directory is not persistent")
        value = {
            "version": 1,
            "members": [member.as_mapping() for member in self._members.values()],
        }
        temporary_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(value, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self._path)


@dataclass
class IdentificationSession:
    call_sid: str
    state: str
    candidate_ids: tuple[str, ...] = ()
    selected_member_id: str | None = None
    attempts: int = 0
    capability_token: str | None = None
    audit_events: list[Mapping[str, Any]] = field(default_factory=list)


class IdentityController:
    EDITABLE_CONTEXT_FIELDS = {
        "mailing_address": 200,
        "contact_phone": 50,
        "primary_care_clinic": 120,
        "primary_care_preference": 120,
        "preferred_language": 50,
        "secondary_language": 50,
    }

    def __init__(self, directory: DemoDirectory, max_attempts: int = 3):
        self.directory = directory
        self.max_attempts = max_attempts
        self._sessions: dict[str, IdentificationSession] = {}
        self._lock = threading.RLock()

    def start(self, call_sid: str, caller_number: str) -> IdentificationSession:
        with self._lock:
            existing = self._sessions.get(call_sid)
            if existing:
                return existing
            candidates = self.directory.candidates(caller_number)
            state = (
                "candidate_unique"
                if len(candidates) == 1
                else "candidate_ambiguous"
                if candidates
                else "unidentified"
            )
            session = IdentificationSession(
                call_sid=call_sid,
                state=state,
                candidate_ids=candidates,
                selected_member_id=candidates[0] if len(candidates) == 1 else None,
            )
            self._audit(session, "candidate_lookup", state, match_count=len(candidates))
            self._sessions[call_sid] = session
            return session

    def session(self, call_sid: str) -> IdentificationSession:
        try:
            return self._sessions[call_sid]
        except KeyError as error:
            raise IdentityError("Unknown or expired call session") from error

    def claim_name(self, call_sid: str, claimed_name: str) -> bool:
        with self._lock:
            session = self.session(call_sid)
            if session.state in {"locked", "ended"}:
                return False
            if session.state == "verified_member":
                return True
            pool = session.candidate_ids or None
            member_id = self.directory.claim_name(claimed_name, pool)
            if not member_id:
                self._audit(session, "name_claim", "no_unique_match")
                return False
            session.selected_member_id = member_id
            session.state = "verification_pending"
            self._audit(session, "name_claim", "candidate_selected")
            return True

    def verify_pin(self, call_sid: str, pin: str) -> IdentificationSession:
        with self._lock:
            session = self.session(call_sid)
            if session.state == "locked":
                return session
            if session.state == "verified_member":
                return session
            if not session.selected_member_id:
                raise IdentityError("No candidate selected for verification")
            session.attempts += 1
            if self.directory.verify_pin(session.selected_member_id, pin):
                session.state = "verified_member"
                session.capability_token = secrets.token_urlsafe(32)
                self._audit(session, "verification", "verified")
            elif session.attempts >= self.max_attempts:
                session.state = "locked"
                self._audit(session, "verification", "locked")
            else:
                session.state = "verification_pending"
                self._audit(session, "verification", "mismatch")
            return session

    def confirm_synthetic_candidate(self, call_sid: str) -> IdentificationSession:
        """Unlock a fictional record after verbal confirmation; never use for real PHI."""
        with self._lock:
            session = self.session(call_sid)
            if session.state == "verified_member":
                return session
            if session.state in {"locked", "ended"} or not session.selected_member_id:
                raise IdentityError("No synthetic candidate selected for confirmation")
            session.state = "verified_member"
            session.capability_token = secrets.token_urlsafe(32)
            self._audit(session, "verification", "synthetic_demo_confirmation")
            return session

    def member_context(self, call_sid: str, capability_token: str) -> Mapping[str, str]:
        with self._lock:
            session = self.session(call_sid)
            if (
                session.state != "verified_member"
                or not session.capability_token
                or not hmac.compare_digest(session.capability_token, capability_token)
                or not session.selected_member_id
            ):
                self._audit(session, "member_context", "blocked")
                raise PermissionError("Member context requires a valid call-bound capability")
            self._audit(session, "member_context", "allowed")
            member = self.directory.member(session.selected_member_id)
            return {"display_name": member.display_name, **dict(member.safe_context)}

    def update_member_context(
        self,
        call_sid: str,
        capability_token: str,
        field_name: str,
        value: str,
        *,
        caller_confirmed: bool,
    ) -> Mapping[str, str]:
        with self._lock:
            session = self.session(call_sid)
            if (
                session.state != "verified_member"
                or not session.capability_token
                or not hmac.compare_digest(session.capability_token, capability_token)
                or not session.selected_member_id
            ):
                self._audit(session, "member_update", "blocked_unverified")
                raise PermissionError("Member updates require verified identity")
            if not caller_confirmed:
                self._audit(session, "member_update", "blocked_unconfirmed")
                raise IdentityError("Update requires explicit caller confirmation")
            maximum_length = self.EDITABLE_CONTEXT_FIELDS.get(field_name)
            clean_value = " ".join(value.split())
            if maximum_length is None:
                self._audit(session, "member_update", "blocked_field")
                raise IdentityError("Unsupported synthetic member field")
            if not clean_value or len(clean_value) > maximum_length:
                self._audit(session, "member_update", "blocked_value", field=field_name)
                raise IdentityError("Invalid synthetic member value")
            if field_name == "contact_phone":
                digits = "".join(character for character in clean_value if character.isdigit())
                if len(digits) < 7:
                    self._audit(
                        session, "member_update", "blocked_value", field=field_name
                    )
                    raise IdentityError("Synthetic contact phone is incomplete")
            member = self.directory.update_safe_context(
                session.selected_member_id, {field_name: clean_value}
            )
            self._audit(session, "member_update", "updated", field=field_name)
            return {"display_name": member.display_name, **dict(member.safe_context)}

    def end(self, call_sid: str) -> None:
        with self._lock:
            session = self._sessions.pop(call_sid, None)
            if session:
                session.capability_token = None
                session.state = "ended"

    @staticmethod
    def _audit(
        session: IdentificationSession, event: str, decision: str, **metadata: Any
    ) -> None:
        session.audit_events.append(
            {"event": event, "decision": decision, "metadata": dict(metadata)}
        )


def seed_member(
    *,
    path: Path,
    pepper: str,
    member_id: str,
    display_name: str,
    phone: str,
    pin: str,
    safe_context: Mapping[str, str],
) -> DemoMember:
    normalized = normalize_phone(phone)
    if not is_e164(normalized):
        raise IdentityError("Phone must be valid E.164")
    if len(pin) != 4 or not pin.isdigit():
        raise IdentityError("Demo PIN must contain exactly four digits")
    members: list[DemoMember] = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        rows = existing.get("members") if isinstance(existing, Mapping) else None
        if not isinstance(rows, list):
            raise IdentityError(f"{path}: expected a members array")
        members = [DemoMember.from_mapping(item) for item in rows]
    record = DemoMember(
        id=member_id,
        display_name=display_name,
        phone_hmacs=(phone_hmac(pepper, normalized),),
        pin_hmac=pin_hmac(pepper, member_id, pin),
        safe_context=dict(safe_context),
    )
    members = [item for item in members if item.id != member_id] + [record]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "members": [item.as_mapping() for item in members]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return record


def phone_hmac(pepper: str, normalized_phone: str) -> str:
    return _token(pepper, f"phone:v1:{normalized_phone}")


def pin_hmac(pepper: str, member_id: str, pin: str) -> str:
    return _token(pepper, f"pin:v1:{member_id}:{pin}")


def _token(pepper: str, value: str) -> str:
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def _normalize_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
