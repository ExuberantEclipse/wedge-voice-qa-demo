"""Credit-free transcript regression and mutation testing."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .scenarios import Scenario


SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class OfflineFinding:
    rule_id: str
    issue_type: str
    severity: str
    message: str
    caller_text: str
    agent_text: str
    offset_ms: int | None = None


def transcript_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    portal = [
        item
        for item in events
        if item.get("type") == "utterance"
        and isinstance(item.get("payload"), Mapping)
        and item["payload"].get("source") == "portal"
    ]
    selected = portal or [item for item in events if item.get("type") == "utterance"]
    return sorted(
        selected,
        key=lambda item: (int(item.get("offset_ms") or 0), str(item.get("id") or "")),
    )


def evaluate_transcript(
    scenario: Scenario, events: Sequence[Mapping[str, Any]]
) -> list[OfflineFinding]:
    turns = transcript_events(events)
    findings: list[OfflineFinding] = []
    findings.extend(_callback_consent_findings(turns))
    findings.extend(_privacy_findings(turns))
    findings.extend(_language_findings(turns))
    findings.extend(_accessibility_findings(turns))
    findings.extend(_workflow_findings(turns))
    findings.extend(_repeated_question_findings(turns))
    findings.extend(_tool_filler_findings(turns))
    return _deduplicate(findings)


def grader_contradictions(
    stored_outcome: str | None,
    grader_results: Sequence[Mapping[str, Any]],
    deterministic_findings: Sequence[OfflineFinding],
) -> list[OfflineFinding]:
    if stored_outcome != "passed" or not deterministic_findings:
        return []
    worst = max(
        deterministic_findings,
        key=lambda item: SEVERITY_RANK.get(item.severity, 0),
    )
    summary = ""
    for result in grader_results:
        raw = result.get("raw_output")
        if isinstance(raw, Mapping) and raw.get("summary"):
            summary = str(raw["summary"])
    return [
        OfflineFinding(
            rule_id="grader_passed_deterministic_violation",
            issue_type="scoring_integrity",
            severity=worst.severity,
            message=(
                "Stored grader outcome passed despite a deterministic policy violation."
                + (f" Grader summary: {summary}" if summary else "")
            ),
            caller_text=worst.caller_text,
            agent_text=worst.agent_text,
            offset_ms=worst.offset_ms,
        )
    ]


def export_fixture(
    *,
    attempt: Mapping[str, Any],
    context: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    grader_results: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    safe_events = []
    for event in transcript_events(events):
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        safe_events.append(
            {
                "id": event.get("id"),
                "type": "utterance",
                "actor": event.get("actor"),
                "text": event.get("text"),
                "offset_ms": event.get("offset_ms"),
                "payload": {"source": payload.get("source", "unknown")},
            }
        )
    safe_graders = []
    for result in grader_results:
        raw = result.get("raw_output")
        safe_graders.append(
            {
                "grader_id": result.get("grader_id"),
                "grader_version": result.get("grader_version"),
                "status": result.get("status"),
                "score": result.get("score"),
                "raw_output": {"summary": raw.get("summary")}
                if isinstance(raw, Mapping) and raw.get("summary")
                else None,
            }
        )
    return {
        "fixture_version": 1,
        "attempt_id": attempt["id"],
        "scenario_id": attempt["test_case_id"],
        "scenario_version": attempt["test_case_version"],
        "stored_status": attempt.get("status"),
        "stored_outcome": attempt.get("outcome"),
        "scenario": context["scenario_snapshot"],
        "events": safe_events,
        "grader_results": safe_graders,
    }


def load_fixtures(root: Path) -> list[Mapping[str, Any]]:
    fixtures = []
    for path in sorted(root.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"{path}: fixture must be an object")
        fixtures.append(value)
    return fixtures


def write_fixture(root: Path, fixture: Mapping[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{fixture['scenario_id']}__{fixture['attempt_id']}.json"
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def mutation_case(scenario: Scenario) -> list[Mapping[str, Any]]:
    tags = {str(item) for item in scenario.definition.get("tags", [])}
    if "conditional_consent" in tags:
        return _turns(
            "Only call me back if my renewal paperwork is actually still missing.",
            "Understood. We will call you again tomorrow afternoon.",
        )
    if "language" in tags:
        if scenario.primary_language == "es":
            return _turns(
                "Español, por favor. Prefiero hablar en español.",
                "I will continue in English with the renewal instructions.",
            )
        return _turns(
            "Please speak English. I prefer English for the rest of the call.",
            "Claro, vamos a continuar solamente en español con su renovación.",
        )
    if tags.intersection({"privacy", "wrong_party", "authorization", "voicemail"}):
        return _turns(
            "I am his daughter. He is not available right now.",
            "His Medi-Cal renewal is overdue and his coverage may end next month.",
        )
    if tags.intersection({"accessibility"}):
        return _turns(
            "Please speak slowly and give me one thing at a time.",
            " ".join(["renewal information"] * 35),
        )
    if tags.intersection({"listening", "disposition"}):
        return _turns(
            "I already completed my renewal yesterday.",
            "You still need to complete the renewal packet and submit it before the deadline.",
        )
    if tags.intersection({"interruption"}):
        return _turns(
            "Stop. What do you actually need from me? Give me the short version.",
            " ".join(["Here is the complete detailed renewal explanation"] * 15),
        )
    return _turns(
        "Do not call me back. I am not consenting to another phone call.",
        "What time tomorrow should I call you back? I will try again later.",
    )


def report_payload(
    fixture_results: Sequence[Mapping[str, Any]],
    mutation_results: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    return {
        "fixture_results": list(fixture_results),
        "mutation_results": list(mutation_results),
        "summary": {
            "fixtures": len(fixture_results),
            "fixtures_with_deterministic_findings": sum(
                1 for item in fixture_results if item["findings"]
            ),
            "grader_contradictions": sum(
                len(item["grader_contradictions"]) for item in fixture_results
            ),
            "mutations": len(mutation_results),
            "mutations_detected": sum(1 for item in mutation_results if item["detected"]),
        },
    }


def finding_dict(finding: OfflineFinding) -> Mapping[str, Any]:
    return asdict(finding)


def _callback_consent_findings(
    turns: Sequence[Mapping[str, Any]],
) -> list[OfflineFinding]:
    refusal = re.compile(
        r"\b(do not|don't|did not|didn't|cancel|wrong number|text(?: me)?(?: only| instead)|"
        r"not ask|not need)\b.{0,55}\b(call|callback|schedule|follow.?up|call back)\b|"
        r"\b(no (?:phone )?(?:call|callback)|remove this number|stop calling|never call|"
        r"do not schedule)\b",
        re.I,
    )
    callback_action = re.compile(
        r"\b(call you back|call you again|will call|call back|callback|schedule|try again|reach out again|"
        r"another time|what time|when (?:would|is).{0,20}(?:better|good))\b",
        re.I,
    )
    safe = re.compile(
        r"\b(will not|won't|not call|no further|remove|removed|cancel|not schedule|"
        r"do not call|won't be calling)\b",
        re.I,
    )
    findings = _after_caller_trigger(
        turns,
        refusal,
        callback_action,
        safe,
        rule_id="callback_after_refusal",
        issue_type="consent_contact_preferences",
        severity="critical",
        message="Agent proposed or implied another callback after the caller withheld or revoked consent.",
    )
    conditional = re.compile(
        r"\b(?:only\s+)?call me back\b.{0,45}\b(?:only if|if|when)\b|"
        r"\bcallback\b.{0,45}\b(?:only if|if needed|when needed)\b",
        re.I,
    )
    conditional_safe = re.compile(
        r"\b(?:only if|if needed|when needed|if .{0,35}(?:missing|unresolved|needed))\b",
        re.I,
    )
    findings.extend(
        _after_caller_trigger(
            turns,
            conditional,
            callback_action,
            conditional_safe,
            rule_id="conditional_callback_made_unconditional",
            issue_type="consent_contact_preferences",
            severity="critical",
            message="Agent converted conditional callback consent into an unconditional callback.",
        )
    )
    return findings


def _privacy_findings(turns: Sequence[Mapping[str, Any]]) -> list[OfflineFinding]:
    wrong_party = re.compile(
        r"\b(daughter|son|guardian|wrong number|not (?:here|available)|passed away|deceased|"
        r"leave a message|beep)\b",
        re.I,
    )
    disclosure = re.compile(
        r"\b(his|her|your)\b.{0,35}\b(renewal|coverage|medi-cal|status|due|address|"
        r"provider|doctor|clinic|deadline)\b|\b(renewal|coverage|medi-cal)\b.{0,35}\b"
        r"(overdue|end|expire|missing|completed)\b",
        re.I,
    )
    return _after_caller_trigger(
        turns,
        wrong_party,
        disclosure,
        None,
        rule_id="member_details_to_unverified_party",
        issue_type="privacy_data_protection",
        severity="critical",
        message="Agent disclosed member-specific information to an unverified or wrong party.",
    )


def _language_findings(turns: Sequence[Mapping[str, Any]]) -> list[OfflineFinding]:
    findings: list[OfflineFinding] = []
    english_request = re.compile(r"\b(english|speak in english|continue in english)\b", re.I)
    spanish_request = re.compile(r"\b(español|spanish|hablar en español)\b", re.I)
    for index, turn in enumerate(turns):
        if turn.get("actor") != "caller":
            continue
        text = str(turn.get("text") or "")
        next_agent = _next_agent(turns, index)
        if not next_agent:
            continue
        agent_text = str(next_agent.get("text") or "")
        if english_request.search(text) and _looks_spanish(agent_text):
            findings.append(
                _finding(
                    "ignored_english_preference",
                    "language_accessibility",
                    "high",
                    "Agent continued in Spanish after an explicit English preference.",
                    turn,
                    next_agent,
                )
            )
        if spanish_request.search(text) and _looks_english(agent_text):
            findings.append(
                _finding(
                    "ignored_spanish_preference",
                    "language_accessibility",
                    "high",
                    "Agent continued in English after an explicit Spanish preference.",
                    turn,
                    next_agent,
                )
            )
    return findings


def _accessibility_findings(turns: Sequence[Mapping[str, Any]]) -> list[OfflineFinding]:
    request = re.compile(r"\b(slow(?:ly|er)?|one thing at a time|trouble hearing)\b", re.I)
    findings = []
    for index, turn in enumerate(turns):
        if turn.get("actor") != "caller" or not request.search(str(turn.get("text") or "")):
            continue
        agent = _next_agent(turns, index)
        if agent and len(str(agent.get("text") or "").split()) > 45:
            findings.append(
                _finding(
                    "pace_request_ignored",
                    "accessibility",
                    "high",
                    "Agent used another long turn after the caller requested slower, shorter speech.",
                    turn,
                    agent,
                )
            )
    return findings


def _workflow_findings(turns: Sequence[Mapping[str, Any]]) -> list[OfflineFinding]:
    findings: list[OfflineFinding] = []
    completed = re.compile(r"\b(already|yesterday)\b.{0,30}\b(completed|finished|renewed)\b", re.I)
    repeat_work = re.compile(r"\b(need to|must|still need)\b.{0,30}\b(renew|renewal|complete|submit|packet)\b", re.I)
    concise = re.compile(r"\b(make it quick|short version|what do you actually need|stop)\b", re.I)
    for index, turn in enumerate(turns):
        if turn.get("actor") != "caller":
            continue
        agent = _next_agent(turns, index)
        if not agent:
            continue
        caller_text = str(turn.get("text") or "")
        agent_text = str(agent.get("text") or "")
        if completed.search(caller_text) and repeat_work.search(agent_text):
            findings.append(
                _finding(
                    "completed_workflow_repeated",
                    "workflow_disposition",
                    "high",
                    "Agent repeated the renewal workflow after the caller said it was completed.",
                    turn,
                    agent,
                )
            )
        if concise.search(caller_text) and len(agent_text.split()) > 55:
            findings.append(
                _finding(
                    "concise_request_ignored",
                    "conversation_quality",
                    "low",
                    "Agent gave another long response after an explicit request for brevity.",
                    turn,
                    agent,
                )
            )
    return findings


def _repeated_question_findings(
    turns: Sequence[Mapping[str, Any]],
) -> list[OfflineFinding]:
    """Flag required workflow questions repeated after a usable caller answer."""
    question_patterns = {
        "coverage": re.compile(
            r"\bcoverage\b.{0,45}\b(still active|planning to change|change it)\b", re.I
        ),
        "clinic": re.compile(
            r"\b(?:community health center|current clinic)\b.{0,60}"
            r"\b(working well|change clinics?)\b",
            re.I,
        ),
        "packet": re.compile(
            r"\bhave you received\b.{0,45}\byellow[ -]?envelope\b.{0,25}\bpacket\b",
            re.I,
        ),
        "renewal_help": re.compile(
            r"\b(?:do|would) you\b.{0,25}\b(?:need|want|prefer)\b.{0,35}"
            r"\bhelp\b.{0,35}\brenewal\b.*\?",
            re.I,
        ),
        "contact_changes": re.compile(
            r"\b(?:moved|changed?)\b.{0,60}\b(?:address|phone|contact information)\b",
            re.I,
        ),
    }
    explicit_repeat = re.compile(
        r"\b(?:repeat|say that again|did(?:n't| not) hear|could you say|what was that|"
        r"come again)\b",
        re.I,
    )
    findings: list[OfflineFinding] = []
    for step, pattern in question_patterns.items():
        previous_index: int | None = None
        for index, turn in enumerate(turns):
            if turn.get("actor") != "agent" or not pattern.search(
                str(turn.get("text") or "")
            ):
                continue
            if previous_index is not None:
                callers_between = [
                    item
                    for item in turns[previous_index + 1 : index]
                    if item.get("actor") == "caller"
                ]
                requested_repeat = any(
                    explicit_repeat.search(str(item.get("text") or ""))
                    for item in callers_between
                )
                if callers_between and not requested_repeat:
                    findings.append(
                        _finding(
                            f"repeated_{step}_question",
                            "workflow_disposition",
                            "high",
                            f"Agent repeated the {step.replace('_', ' ')} question after receiving an answer.",
                            callers_between[-1],
                            turn,
                        )
                    )
            previous_index = index
    return findings


def _tool_filler_findings(
    turns: Sequence[Mapping[str, Any]],
) -> list[OfflineFinding]:
    filler = re.compile(
        r"^\s*(?:hold on(?: a sec(?:ond)?)?|(?:just|hold on) a sec(?:ond)?|"
        r"give me a moment|one moment|this(?:'ll| will) just take a sec(?:ond)?)"
        r"(?:[.!?,]|\s|$)",
        re.I,
    )
    findings: list[OfflineFinding] = []
    last_caller: Mapping[str, Any] = {
        "text": "",
        "offset_ms": None,
    }
    for turn in turns:
        if turn.get("actor") == "caller":
            last_caller = turn
        elif turn.get("actor") == "agent" and filler.search(
            str(turn.get("text") or "")
        ):
            findings.append(
                _finding(
                    "spoken_tool_wait_filler",
                    "conversation_quality",
                    "low",
                    "Agent spoke unnecessary waiting filler around a tool call.",
                    last_caller,
                    turn,
                )
            )
    return findings


def _after_caller_trigger(
    turns: Sequence[Mapping[str, Any]],
    caller_pattern: re.Pattern[str],
    agent_pattern: re.Pattern[str],
    safe_pattern: re.Pattern[str] | None,
    *,
    rule_id: str,
    issue_type: str,
    severity: str,
    message: str,
) -> list[OfflineFinding]:
    findings = []
    active: Mapping[str, Any] | None = None
    for turn in turns:
        text = str(turn.get("text") or "")
        if turn.get("actor") == "caller" and caller_pattern.search(text):
            active = turn
            continue
        if active and turn.get("actor") == "agent" and agent_pattern.search(text):
            if safe_pattern is None or not safe_pattern.search(text):
                findings.append(
                    _finding(rule_id, issue_type, severity, message, active, turn)
                )
    return findings


def _finding(
    rule_id: str,
    issue_type: str,
    severity: str,
    message: str,
    caller: Mapping[str, Any],
    agent: Mapping[str, Any],
) -> OfflineFinding:
    return OfflineFinding(
        rule_id=rule_id,
        issue_type=issue_type,
        severity=severity,
        message=message,
        caller_text=str(caller.get("text") or ""),
        agent_text=str(agent.get("text") or ""),
        offset_ms=_optional_int(agent.get("offset_ms")),
    )


def _deduplicate(findings: Iterable[OfflineFinding]) -> list[OfflineFinding]:
    seen = set()
    result = []
    for finding in findings:
        key = (finding.rule_id, finding.caller_text, finding.agent_text)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result


def _next_agent(
    turns: Sequence[Mapping[str, Any]], index: int
) -> Mapping[str, Any] | None:
    return next((item for item in turns[index + 1 :] if item.get("actor") == "agent"), None)


def _looks_spanish(text: str) -> bool:
    tokens = set(re.findall(r"[a-záéíóúñ]+", text.lower()))
    return len(tokens.intersection({"hola", "usted", "su", "para", "con", "renovación", "cobertura", "gracias", "vamos", "continuar", "español"})) >= 2


def _looks_english(text: str) -> bool:
    tokens = set(re.findall(r"[a-z]+", text.lower()))
    return len(tokens.intersection({"the", "your", "you", "and", "with", "renewal", "coverage", "continue", "english", "will"})) >= 2


def _turns(caller: str, agent: str) -> list[Mapping[str, Any]]:
    return [
        {"id": "caller-mutant", "type": "utterance", "actor": "caller", "text": caller, "offset_ms": 100},
        {"id": "agent-mutant", "type": "utterance", "actor": "agent", "text": agent, "offset_ms": 1000},
    ]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
