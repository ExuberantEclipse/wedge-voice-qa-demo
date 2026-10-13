"""Run LiveKit-inference graders and persist scorecards and findings."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from livekit.agents import inference, llm

from .grading import Scorecard, grade_call
from .scenarios import Scenario, ScenarioValidationError, validate_definition
from .store import SupabaseStore


def scenario_from_definition(definition: Mapping[str, Any], source: str) -> Scenario:
    errors = validate_definition(definition, source)
    if errors:
        raise ScenarioValidationError("\n".join(errors))
    return Scenario(
        id=str(definition["id"]),
        version=int(definition["version"]),
        title=str(definition["title"]),
        priority=str(definition["priority"]),
        definition=definition,
    )


async def score_events(
    store: SupabaseStore,
    attempt_id: str,
    scenario: Scenario,
    events: Sequence[Mapping[str, Any]],
    grader_model: str,
) -> Scorecard:
    model = inference.LLM(model=grader_model)

    async def complete(prompt: str) -> str:
        context = llm.ChatContext.empty()
        context.add_message(
            role="system",
            content=(
                "You are a high-precision QA grader. Follow the requested JSON schema "
                "exactly and never invent evidence."
            ),
        )
        context.add_message(role="user", content=prompt)
        response = await model.chat(chat_ctx=context).collect()
        return response.text

    scorecard = await grade_call(scenario, events, complete)
    await asyncio.to_thread(_persist_scorecard, store, attempt_id, scorecard)
    return scorecard


async def score_attempt(
    store: SupabaseStore, attempt_id: str, grader_model: str
) -> Scorecard:
    context = await asyncio.to_thread(store.attempt_context, attempt_id)
    scenario = scenario_from_definition(
        context["scenario_snapshot"], f"attempt {attempt_id}"
    )
    events = await asyncio.to_thread(store.events_for_attempt, attempt_id)
    return await score_events(store, attempt_id, scenario, events, grader_model)


def _persist_scorecard(
    store: SupabaseStore, attempt_id: str, scorecard: Scorecard
) -> None:
    for result in scorecard.results:
        store.upsert_grader_result(
            attempt_id=attempt_id,
            grader_id=result.grader_id,
            grader_version=result.grader_version,
            status=result.status,
            score=result.score,
            evidence=result.evidence,
            raw_output=result.raw_output,
        )
    active_finding_ids: list[str] = []
    for candidate in scorecard.findings:
        finding = store.upsert_finding(
            signature=candidate.signature,
            title=candidate.title,
            category=candidate.category,
            severity=candidate.severity,
            actual_behavior=candidate.actual_behavior,
            expected_behavior=candidate.expected_behavior,
            reproduction_steps=candidate.reproduction_steps,
        )
        finding_id = str(finding["id"])
        store.link_finding(
            finding_id,
            attempt_id,
            candidate.evidence_note,
            candidate.offset_ms,
        )
        active_finding_ids.append(finding_id)
    store.remove_stale_finding_occurrences(attempt_id, active_finding_ids)
    store.set_attempt_outcome(attempt_id, scorecard.outcome)
