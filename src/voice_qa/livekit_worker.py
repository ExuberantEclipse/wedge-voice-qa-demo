"""LiveKit inbound worker that behaves as the queued synthetic member."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Mapping, Optional

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    TurnHandlingOptions,
    inference,
    llm,
)

from .config import Settings, normalize_phone
from .prompts import build_caller_instructions, build_unassigned_instructions
from .scenarios import Scenario, ScenarioValidationError, validate_definition
from .scoring_service import score_events
from .store import Assignment, StoreError, SupabaseStore


load_dotenv(".env.local")
logger = logging.getLogger("voice_qa.worker")
server = AgentServer()


class SyntheticMember(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


def _attribute(attributes: Mapping[str, str], *names: str) -> Optional[str]:
    for name in names:
        value = attributes.get(name)
        if value:
            return value
    return None


def _scenario_from_assignment(assignment: Assignment) -> Scenario:
    definition = assignment.scenario_snapshot
    errors = validate_definition(definition, f"assignment {assignment.id}")
    if errors:
        raise ScenarioValidationError("\n".join(errors))
    return Scenario(
        id=str(definition["id"]),
        version=int(definition["version"]),
        title=str(definition["title"]),
        priority=str(definition["priority"]),
        definition=definition,
    )


@server.rtc_session(agent_name=os.getenv("QA_AGENT_NAME", "qa-simulated-caller"))
async def simulated_member(ctx: agents.JobContext) -> None:
    settings = Settings.from_env(require_supabase=True)
    store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)

    await ctx.connect()
    sip_participant = await ctx.wait_for_participant()
    attributes: Mapping[str, str] = sip_participant.attributes or {}
    called_number = normalize_phone(
        _attribute(
            attributes,
            "sip.trunkPhoneNumber",
            "sip.trunk_phone_number",
            "sip.toPhoneNumber",
        )
        or settings.called_number
        or ""
    )
    sip_call_id = _attribute(attributes, "sip.callID", "sip.callId", "sip.call_id")

    if not called_number:
        logger.error("Inbound call has no dialed-number attribute and QA_CALLED_NUMBER is unset")
        instructions = build_unassigned_instructions()
        assignment = None
        scenario = None
    else:
        try:
            assignment = await asyncio.to_thread(
                store.claim_next, called_number, ctx.room.name, sip_call_id
            )
        except StoreError:
            logger.exception("Could not claim a scenario for %s", called_number)
            assignment = None
        if assignment is None:
            logger.warning("No queued scenario for %s", called_number)
            instructions = build_unassigned_instructions()
            scenario = None
        else:
            try:
                scenario = _scenario_from_assignment(assignment)
                instructions = build_caller_instructions(scenario)
            except (ScenarioValidationError, KeyError, TypeError, ValueError) as error:
                logger.exception("Claimed assignment %s is invalid", assignment.id)
                await asyncio.to_thread(
                    store.fail_assignment,
                    assignment.id,
                    assignment.test_run_id,
                    str(error),
                )
                assignment = None
                scenario = None
                instructions = build_unassigned_instructions()

    attempt_id: Optional[str] = None
    if assignment is not None:
        try:
            attempt_id = await asyncio.to_thread(
                store.start_attempt, assignment, ctx.room.name, sip_call_id
            )
            await asyncio.to_thread(
                store.append_event,
                attempt_id,
                "call_connected",
                "system",
                {
                    "room": ctx.room.name,
                    "sip_call_id": sip_call_id,
                    "called_number": called_number,
                    "scenario_id": assignment.test_case_id,
                    "scenario_version": assignment.test_case_version,
                },
            )
        except StoreError as error:
            logger.exception("Could not create an attempt for assignment %s", assignment.id)
            try:
                await asyncio.to_thread(
                    store.fail_assignment,
                    assignment.id,
                    assignment.test_run_id,
                    str(error),
                )
            except StoreError:
                logger.exception("Could not mark assignment %s failed", assignment.id)
            assignment = None
            scenario = None
            instructions = build_unassigned_instructions()

    completion = {"done": False}
    call_started_at = time.time()
    captured_events: list[dict[str, Any]] = []
    duration_task: Optional[asyncio.Task[None]] = None
    language = scenario.primary_language if scenario is not None else "en"

    session = AgentSession(
        stt=inference.STT(model=settings.stt_model, language=language),
        llm=inference.LLM(model=settings.llm_model),
        tts=inference.TTS(model=settings.tts_model, voice=settings.tts_voice),
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()),
    )

    def offset_ms(created_at: float) -> int:
        return max(0, int((created_at - call_started_at) * 1000))

    @session.on("conversation_item_added")
    def on_conversation_item(event: Any) -> None:
        item = event.item
        if not isinstance(item, llm.ChatMessage) or item.role not in {"user", "assistant"}:
            return
        text = item.text_content
        if not text:
            return
        captured_events.append(
            {
                "type": "utterance",
                "actor": "agent" if item.role == "user" else "caller",
                "text": text,
                "offset_ms": offset_ms(event.created_at),
                "payload": {
                    "source": "livekit",
                    "livekit_item_id": item.id,
                    "livekit_role": item.role,
                    "interrupted": item.interrupted,
                    "confidence": item.transcript_confidence,
                    "metrics": dict(item.metrics),
                },
            }
        )

    @session.on("error")
    def on_session_error(event: Any) -> None:
        serialized = event.model_dump(mode="json")
        error_data = serialized.get("error")
        if isinstance(error_data, Mapping):
            error_text = ": ".join(
                str(value)
                for value in (error_data.get("type"), error_data.get("label"))
                if value
            )
        else:
            error_text = str(error_data or "LiveKit session error")
        captured_events.append(
            {
                "type": "runtime_error",
                "actor": "system",
                "text": error_text,
                "offset_ms": offset_ms(event.created_at),
                "payload": {"source": "livekit", **serialized},
            }
        )

    @session.on("close")
    def on_session_close(event: Any) -> None:
        serialized = event.model_dump(mode="json")
        captured_events.append(
            {
                "type": "session_closed",
                "actor": "system",
                "text": str(serialized.get("reason") or "session closed"),
                "offset_ms": offset_ms(event.created_at),
                "payload": {"source": "livekit", **serialized},
            }
        )

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: Any) -> None:
        if participant.identity != sip_participant.identity or completion["done"]:
            return
        captured_events.append(
            {
                "type": "remote_participant_disconnected",
                "actor": "system",
                "text": None,
                "offset_ms": offset_ms(time.time()),
                "payload": {
                    "source": "livekit",
                    "participant_identity": participant.identity,
                },
            }
        )
        session.shutdown(drain=False)

    async def enforce_max_duration() -> None:
        if scenario is None:
            return
        max_duration = int(scenario.definition.get("max_duration_seconds", 240))
        await asyncio.sleep(max_duration)
        if completion["done"]:
            return
        captured_events.append(
            {
                "type": "max_duration_reached",
                "actor": "system",
                "text": f"Scenario duration limit reached after {max_duration}s",
                "offset_ms": offset_ms(time.time()),
                "payload": {"source": "voice_qa", "max_duration_seconds": max_duration},
            }
        )
        session.shutdown(drain=False)

    async def finish() -> None:
        nonlocal duration_task
        if completion["done"] or assignment is None or attempt_id is None:
            return
        completion["done"] = True
        if duration_task is not None:
            duration_task.cancel()
        try:
            captured_events.append(
                {
                    "type": "call_disconnected",
                    "actor": "system",
                    "text": None,
                    "offset_ms": offset_ms(time.time()),
                    "payload": {
                        "source": "livekit",
                        "room": ctx.room.name,
                        "sip_call_id": sip_call_id,
                    },
                }
            )
            persisted_events = await asyncio.to_thread(
                store.append_events, attempt_id, captured_events
            )
            if scenario is not None:
                try:
                    scorecard = await score_events(
                        store,
                        attempt_id,
                        scenario,
                        persisted_events,
                        settings.grader_model,
                    )
                    logger.info(
                        "Scored attempt %s: %s (%s)",
                        attempt_id,
                        scorecard.outcome,
                        scorecard.summary,
                    )
                except Exception as error:
                    logger.exception("Could not score attempt %s", attempt_id)
                    await asyncio.to_thread(
                        store.append_event,
                        attempt_id,
                        "scoring_error",
                        "system",
                        {"source": "grader", "error_type": type(error).__name__},
                        str(error),
                        offset_ms(time.time()),
                    )
            await asyncio.to_thread(
                store.finish_attempt,
                assignment.id,
                attempt_id,
                assignment.test_run_id,
                "completed",
                None,
            )
        except StoreError:
            logger.exception("Could not finalize attempt %s", attempt_id)

    ctx.add_shutdown_callback(finish)
    try:
        await session.start(room=ctx.room, agent=SyntheticMember(instructions))
        duration_task = asyncio.create_task(enforce_max_duration())
        # Do not call generate_reply here. The outbound Wedge agent should speak first.
    except Exception as error:
        if assignment is not None and attempt_id is not None:
            completion["done"] = True
            await asyncio.to_thread(
                store.finish_attempt,
                assignment.id,
                attempt_id,
                assignment.test_run_id,
                "failed",
                str(error),
            )
        raise


def main() -> None:
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
