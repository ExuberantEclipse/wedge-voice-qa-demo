"""Small PostgREST client for the server-side QA queue."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .scenarios import Scenario


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Assignment:
    id: str
    test_run_id: str
    test_case_id: str
    test_case_version: int
    called_number: str
    scenario_snapshot: Mapping[str, Any]


class SupabaseStore:
    """Uses Supabase's REST surface without exposing its service key to a browser."""

    def __init__(self, url: str, service_role_key: str, timeout_seconds: int = 15):
        self._base_url = url.rstrip("/") + "/rest/v1/"
        self._key = service_role_key
        self._timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
        prefer: Optional[str] = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer

        request = Request(self._base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                response_body = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise StoreError(f"Supabase {method} {path} failed: {error.code} {detail}") from error
        except OSError as error:
            raise StoreError(f"Supabase {method} {path} failed: {error}") from error

        if not response_body:
            return None
        return json.loads(response_body.decode("utf-8"))

    def upsert_test_case(self, scenario: Scenario) -> Mapping[str, Any]:
        path = "test_cases?on_conflict=id,version"
        result = self._request(
            "POST",
            path,
            scenario.as_record(),
            prefer="resolution=merge-duplicates,return=representation",
        )
        return _one(result, "upsert test case")

    def upsert_test_cases(self, scenarios: Sequence[Scenario]) -> List[Mapping[str, Any]]:
        """Upsert a validated scenario catalog in one PostgREST request."""

        if not scenarios:
            return []
        path = "test_cases?on_conflict=id,version"
        result = self._request(
            "POST",
            path,
            [scenario.as_record() for scenario in scenarios],
            prefer="resolution=merge-duplicates,return=representation",
        )
        if not isinstance(result, list) or not all(
            isinstance(row, Mapping) for row in result
        ):
            raise StoreError(f"Expected rows from seed test cases, received {result!r}")
        if len(result) != len(scenarios):
            raise StoreError(
                f"Expected {len(scenarios)} seeded test cases, received {len(result)}"
            )
        return result

    def create_run(
        self,
        label: str,
        agent_version: Optional[str],
        mode: str = "automated_inbound",
    ) -> Mapping[str, Any]:
        result = self._request(
            "POST",
            "test_runs",
            {
                "label": label,
                "mode": mode,
                "status": "queued",
                "agent_version_label": agent_version,
            },
            prefer="return=representation",
        )
        return _one(result, "create run")

    def start_manual_attempt(
        self, assignment: Mapping[str, Any], external_call_id: str
    ) -> str:
        """Claim a manual assignment without a LiveKit room and start its attempt."""

        assignment_id = str(assignment["id"])
        test_run_id = str(assignment["test_run_id"])
        now = utc_now()
        self._request(
            "PATCH",
            "call_assignments?" + urlencode({"id": f"eq.{assignment_id}"}),
            {"status": "claimed", "claimed_at": now},
            prefer="return=minimal",
        )
        self._request(
            "PATCH",
            "test_runs?" + urlencode({"id": f"eq.{test_run_id}"}),
            {"status": "running", "started_at": now},
            prefer="return=minimal",
        )
        result = self._request(
            "POST",
            "call_attempts",
            {
                "assignment_id": assignment_id,
                "test_run_id": test_run_id,
                "test_case_id": assignment["test_case_id"],
                "test_case_version": assignment["test_case_version"],
                "external_call_id": external_call_id,
                "status": "in_progress",
                "started_at": now,
            },
            prefer="return=representation",
        )
        return str(_one(result, "start manual attempt")["id"])

    def queue_assignment(
        self, scenario: Scenario, test_run_id: str, called_number: str
    ) -> Mapping[str, Any]:
        result = self._request(
            "POST",
            "call_assignments",
            {
                "test_run_id": test_run_id,
                "test_case_id": scenario.id,
                "test_case_version": scenario.version,
                "called_number": called_number,
                "scenario_snapshot": dict(scenario.definition),
                "status": "queued",
            },
            prefer="return=representation",
        )
        return _one(result, "queue assignment")

    def claim_next(
        self, called_number: str, room_name: str, sip_call_id: Optional[str]
    ) -> Optional[Assignment]:
        result = self._request(
            "POST",
            "rpc/claim_next_call_assignment",
            {
                "p_called_number": called_number,
                "p_room_name": room_name,
                "p_sip_call_id": sip_call_id,
            },
        )
        if not result:
            return None
        row = _one(result, "claim assignment")
        return Assignment(
            id=row["id"],
            test_run_id=row["test_run_id"],
            test_case_id=row["test_case_id"],
            test_case_version=row["test_case_version"],
            called_number=row["called_number"],
            scenario_snapshot=row["scenario_snapshot"],
        )

    def start_attempt(
        self,
        assignment: Assignment,
        room_name: str,
        sip_call_id: Optional[str],
    ) -> str:
        result = self._request(
            "POST",
            "call_attempts",
            {
                "assignment_id": assignment.id,
                "test_run_id": assignment.test_run_id,
                "test_case_id": assignment.test_case_id,
                "test_case_version": assignment.test_case_version,
                "livekit_room": room_name,
                "sip_call_id": sip_call_id,
                "status": "in_progress",
                "started_at": utc_now(),
            },
            prefer="return=representation",
        )
        return str(_one(result, "start attempt")["id"])

    def finish_attempt(
        self,
        assignment_id: str,
        attempt_id: str,
        test_run_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        now = utc_now()
        self._request(
            "PATCH",
            "call_attempts?" + urlencode({"id": f"eq.{attempt_id}"}),
            {"status": status, "ended_at": now, "error": error},
            prefer="return=minimal",
        )
        assignment_status = "completed" if status == "completed" else "failed"
        self._request(
            "PATCH",
            "call_assignments?" + urlencode({"id": f"eq.{assignment_id}"}),
            {"status": assignment_status, "completed_at": now, "error": error},
            prefer="return=minimal",
        )
        self.refresh_run_status(test_run_id)

    def fail_assignment(self, assignment_id: str, test_run_id: str, error: str) -> None:
        now = utc_now()
        self._request(
            "PATCH",
            "call_assignments?" + urlencode({"id": f"eq.{assignment_id}"}),
            {"status": "failed", "completed_at": now, "error": error},
            prefer="return=minimal",
        )
        self.refresh_run_status(test_run_id)

    def append_event(
        self,
        attempt_id: str,
        event_type: str,
        actor: str,
        payload: Optional[Mapping[str, Any]] = None,
        text: Optional[str] = None,
        offset_ms: Optional[int] = None,
    ) -> Mapping[str, Any]:
        result = self._request(
            "POST",
            "call_events",
            {
                "call_attempt_id": attempt_id,
                "type": event_type,
                "actor": actor,
                "text": text,
                "offset_ms": offset_ms,
                "payload": dict(payload or {}),
            },
            prefer="return=representation",
        )
        return _one(result, "append event")

    def append_events(
        self, attempt_id: str, events: Sequence[Mapping[str, Any]]
    ) -> List[Mapping[str, Any]]:
        if not events:
            return []
        records = []
        for event in events:
            records.append(
                {
                    "call_attempt_id": attempt_id,
                    "type": event["type"],
                    "actor": event["actor"],
                    "text": event.get("text"),
                    "offset_ms": event.get("offset_ms"),
                    "payload": dict(event.get("payload") or {}),
                }
            )
        result = self._request(
            "POST", "call_events", records, prefer="return=representation"
        )
        if not isinstance(result, list) or not all(isinstance(row, Mapping) for row in result):
            raise StoreError(f"Expected event rows, received {result!r}")
        return list(result)

    def upsert_grader_result(
        self,
        attempt_id: str,
        grader_id: str,
        grader_version: str,
        status: str,
        score: Optional[float],
        evidence: Sequence[Mapping[str, Any]],
        raw_output: Optional[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        result = self._request(
            "POST",
            "grader_results?on_conflict=call_attempt_id,grader_id,grader_version",
            {
                "call_attempt_id": attempt_id,
                "grader_id": grader_id,
                "grader_version": grader_version,
                "status": status,
                "score": score,
                "evidence": list(evidence),
                "raw_output": dict(raw_output) if raw_output is not None else None,
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return _one(result, "upsert grader result")

    def upsert_finding(
        self,
        *,
        signature: str,
        title: str,
        category: str,
        severity: str,
        actual_behavior: str,
        expected_behavior: str,
        reproduction_steps: str,
    ) -> Mapping[str, Any]:
        result = self._request(
            "POST",
            "findings?on_conflict=signature",
            {
                "signature": signature,
                "title": title,
                "category": category,
                "severity": severity,
                "actual_behavior": actual_behavior,
                "expected_behavior": expected_behavior,
                "reproduction_steps": reproduction_steps,
                "updated_at": utc_now(),
            },
            prefer="resolution=merge-duplicates,return=representation",
        )
        return _one(result, "upsert finding")

    def link_finding(
        self,
        finding_id: str,
        attempt_id: str,
        evidence_note: str,
        offset_ms: Optional[int],
    ) -> Optional[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": "id",
                "finding_id": f"eq.{finding_id}",
                "call_attempt_id": f"eq.{attempt_id}",
                "limit": "1",
            }
        )
        existing = self._request("GET", "finding_occurrences?" + query)
        if existing:
            result = self._request(
                "PATCH",
                "finding_occurrences?"
                + urlencode({"id": f"eq.{existing[0]['id']}"}),
                {"offset_ms": offset_ms, "evidence_note": evidence_note},
                prefer="return=representation",
            )
            return _one(result, "update finding occurrence")
        result = self._request(
            "POST",
            "finding_occurrences",
            {
                "finding_id": finding_id,
                "call_attempt_id": attempt_id,
                "offset_ms": offset_ms,
                "evidence_note": evidence_note,
            },
            prefer="return=representation",
        )
        return _one(result, "link finding")

    def remove_stale_finding_occurrences(
        self, attempt_id: str, active_finding_ids: Sequence[str]
    ) -> None:
        filters = {"call_attempt_id": f"eq.{attempt_id}"}
        if active_finding_ids:
            filters["finding_id"] = f"not.in.({','.join(active_finding_ids)})"
        self._request(
            "DELETE",
            "finding_occurrences?" + urlencode(filters),
            prefer="return=minimal",
        )

    def set_attempt_outcome(self, attempt_id: str, outcome: str) -> None:
        self._request(
            "PATCH",
            "call_attempts?" + urlencode({"id": f"eq.{attempt_id}"}),
            {"outcome": outcome},
            prefer="return=minimal",
        )

    def refresh_run_status(self, test_run_id: str) -> str:
        assignments = self._request(
            "GET",
            "call_assignments?"
            + urlencode(
                {
                    "select": "status",
                    "test_run_id": f"eq.{test_run_id}",
                }
            ),
        )
        statuses = {str(row["status"]) for row in assignments or []}
        terminal = {"completed", "failed", "cancelled"}
        now = utc_now()
        if statuses and statuses.issubset(terminal):
            status = "failed" if "failed" in statuses else "completed"
            payload: Mapping[str, Any] = {"status": status, "completed_at": now}
        elif "claimed" in statuses or statuses.intersection(terminal):
            status = "running"
            payload = {"status": status, "completed_at": None}
        else:
            status = "queued"
            payload = {"status": status, "completed_at": None}
        self._request(
            "PATCH",
            "test_runs?" + urlencode({"id": f"eq.{test_run_id}"}),
            payload,
            prefer="return=minimal",
        )
        return status

    def events_for_attempt(self, attempt_id: str) -> List[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": "id,sequence,offset_ms,type,actor,text,payload,created_at",
                "call_attempt_id": f"eq.{attempt_id}",
                "order": "sequence.asc",
            }
        )
        return list(self._request("GET", "call_events?" + query) or [])

    def attempt_context(self, attempt_id: str) -> Mapping[str, Any]:
        attempts = self._request(
            "GET",
            "call_attempts?"
            + urlencode(
                {
                    "select": "id,assignment_id,test_run_id,test_case_id,test_case_version,status,outcome,error",
                    "id": f"eq.{attempt_id}",
                    "limit": "1",
                }
            ),
        )
        attempt = _one(attempts, "get attempt")
        assignments = self._request(
            "GET",
            "call_assignments?"
            + urlencode(
                {
                    "select": "scenario_snapshot",
                    "id": f"eq.{attempt['assignment_id']}",
                    "limit": "1",
                }
            ),
        )
        assignment = _one(assignments, "get attempt assignment")
        return {**dict(attempt), "scenario_snapshot": assignment["scenario_snapshot"]}

    def recent_attempts(self, limit: int = 20) -> List[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": (
                    "id,assignment_id,test_run_id,test_case_id,test_case_version,external_call_id,"
                    "livekit_room,sip_call_id,status,outcome,ended_reason,error,"
                    "started_at,ended_at"
                ),
                "order": "started_at.desc",
                "limit": str(limit),
            }
        )
        return list(self._request("GET", "call_attempts?" + query) or [])

    def assignments_for_ids(
        self, assignment_ids: Sequence[str]
    ) -> List[Mapping[str, Any]]:
        if not assignment_ids:
            return []
        query = urlencode(
            {
                "select": "id,test_case_id,test_case_version,scenario_snapshot",
                "id": f"in.({','.join(assignment_ids)})",
            }
        )
        return list(self._request("GET", "call_assignments?" + query) or [])

    def findings(self, limit: int = 500) -> List[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": (
                    "id,signature,title,category,severity,status,actual_behavior,"
                    "expected_behavior,reproduction_steps,suspected_cause,created_at,updated_at"
                ),
                "order": "updated_at.desc",
                "limit": str(limit),
            }
        )
        return list(self._request("GET", "findings?" + query) or [])

    def finding_occurrences_for_attempts(
        self, attempt_ids: Sequence[str]
    ) -> List[Mapping[str, Any]]:
        if not attempt_ids:
            return []
        query = urlencode(
            {
                "select": (
                    "id,finding_id,call_attempt_id,artifact_id,offset_ms,"
                    "evidence_note,created_at"
                ),
                "call_attempt_id": f"in.({','.join(attempt_ids)})",
                "order": "created_at.desc",
                "limit": "5000",
            }
        )
        return list(self._request("GET", "finding_occurrences?" + query) or [])

    def events_for_attempts(
        self, attempt_ids: Sequence[str]
    ) -> List[Mapping[str, Any]]:
        if not attempt_ids:
            return []
        query = urlencode(
            {
                "select": (
                    "id,call_attempt_id,sequence,offset_ms,type,actor,text,payload,created_at"
                ),
                "call_attempt_id": f"in.({','.join(attempt_ids)})",
                "order": "call_attempt_id.asc,sequence.asc",
                "limit": "5000",
            }
        )
        return list(self._request("GET", "call_events?" + query) or [])

    def artifacts_for_attempts(
        self, attempt_ids: Sequence[str]
    ) -> List[Mapping[str, Any]]:
        if not attempt_ids:
            return []
        query = urlencode(
            {
                "select": "id,call_attempt_id,kind,uri,metadata,created_at",
                "call_attempt_id": f"in.({','.join(attempt_ids)})",
                "order": "created_at.asc",
                "limit": "5000",
            }
        )
        return list(self._request("GET", "call_artifacts?" + query) or [])

    def runs_for_ids(self, run_ids: Sequence[str]) -> List[Mapping[str, Any]]:
        if not run_ids:
            return []
        query = urlencode(
            {
                "select": "id,label,mode,status,agent_version_label,created_by,created_at",
                "id": f"in.({','.join(run_ids)})",
                "order": "created_at.desc",
            }
        )
        return list(self._request("GET", "test_runs?" + query) or [])

    def grader_results_for_attempts(
        self, attempt_ids: Sequence[str]
    ) -> List[Mapping[str, Any]]:
        if not attempt_ids:
            return []
        joined = ",".join(attempt_ids)
        query = urlencode(
            {
                "select": "call_attempt_id,grader_id,grader_version,status,score,evidence,raw_output,created_at",
                "call_attempt_id": f"in.({joined})",
                "order": "created_at.asc",
            }
        )
        return list(self._request("GET", "grader_results?" + query) or [])

    def attempt_for_assignment(
        self, assignment_id: str
    ) -> Optional[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": "id,assignment_id,test_run_id,status,outcome,started_at,ended_at",
                "assignment_id": f"eq.{assignment_id}",
                "limit": "1",
            }
        )
        rows = self._request("GET", "call_attempts?" + query) or []
        return rows[0] if rows else None

    def ingest_portal_call(
        self, attempt_id: str, call: Mapping[str, Any]
    ) -> List[Mapping[str, Any]]:
        call_id = str(call.get("id") or "")
        self._request(
            "PATCH",
            "call_attempts?" + urlencode({"id": f"eq.{attempt_id}"}),
            {
                "external_call_id": call_id or None,
                "outcome": call.get("outcome"),
                "ended_reason": call.get("ended_reason"),
            },
            prefer="return=minimal",
        )
        events: List[Mapping[str, Any]] = []
        messages = call.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, Mapping):
                    continue
                role = str(message.get("role") or "")
                offset = _seconds_to_ms(message.get("secondsFromStart"))
                if role in {"bot", "user"}:
                    events.append(
                        {
                            "type": "utterance",
                            "actor": "agent" if role == "bot" else "caller",
                            "text": str(message.get("message") or ""),
                            "offset_ms": offset,
                            "payload": {
                                "source": "portal",
                                "external_call_id": call_id,
                                "portal_role": role,
                            },
                        }
                    )
                elif role == "tool_calls":
                    tool_calls = message.get("toolCalls")
                    events.append(
                        {
                            "type": "tool_call",
                            "actor": "agent",
                            "text": _tool_names(tool_calls),
                            "offset_ms": offset,
                            "payload": {
                                "source": "portal",
                                "external_call_id": call_id,
                                "tool_calls": tool_calls if isinstance(tool_calls, list) else [],
                            },
                        }
                    )
        events.append(
            {
                "type": "portal_call_ingested",
                "actor": "system",
                "text": str(call.get("summary") or ""),
                "offset_ms": None,
                "payload": {
                    "source": "portal",
                    "external_call_id": call_id,
                    "status": call.get("status"),
                    "outcome": call.get("outcome"),
                    "ended_reason": call.get("ended_reason"),
                    "reached": call.get("reached"),
                    "next_step": call.get("next_step"),
                    "collected": call.get("collected") or {},
                    "recording_url": call.get("recording_url"),
                },
            }
        )
        return self.append_events(attempt_id, events)

    def recent_assignments(self, limit: int = 20) -> List[Mapping[str, Any]]:
        query = urlencode(
            {
                "select": "id,test_case_id,test_case_version,called_number,status,created_at,claimed_at,completed_at,error",
                "order": "created_at.desc",
                "limit": str(limit),
            }
        )
        result = self._request("GET", "call_assignments?" + query)
        return list(result or [])


def _one(result: Any, operation: str) -> Mapping[str, Any]:
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], Mapping):
        return result[0]
    if isinstance(result, Mapping):
        return result
    raise StoreError(f"Expected one row from {operation}, received {result!r}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_to_ms(value: Any) -> Optional[int]:
    try:
        return max(0, int(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _tool_names(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    names = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        function = item.get("function")
        if isinstance(function, Mapping) and function.get("name"):
            names.append(str(function["name"]))
    return ", ".join(names)
