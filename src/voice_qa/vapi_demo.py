"""Vapi inbound demo for caller-number-assisted member identification."""

from __future__ import annotations

import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

from .caller_identity import DemoDirectory, IdentityController, IdentityError, seed_member
from .config import project_root
from .renewal_workflow import RenewalWorkflowController, WorkflowError


VAPI_API_BASE = "https://api.vapi.ai"


def create_app(
    *,
    controller: IdentityController,
    webhook_secret: str,
    workflow: RenewalWorkflowController | None = None,
) -> Flask:
    if len(webhook_secret) < 24:
        raise IdentityError("VAPI_WEBHOOK_SECRET must contain at least 24 characters")
    app = Flask(__name__)
    renewal = workflow or RenewalWorkflowController()

    def authorized() -> bool:
        expected = f"Bearer {webhook_secret}"
        actual = request.headers.get("Authorization", "")
        legacy = request.headers.get("X-Vapi-Secret", "")
        return hmac.compare_digest(actual, expected) or hmac.compare_digest(
            legacy, webhook_secret
        )

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"status": "ok", "demo": "vapi-caller-identification"})

    @app.post("/vapi/webhook")
    def webhook() -> Response:
        if not authorized():
            return jsonify({"error": "unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        message = body.get("message") if isinstance(body, Mapping) else None
        if not isinstance(message, Mapping):
            return jsonify({"error": "invalid message"}), 400
        message_type = str(message.get("type") or "")

        if message_type == "assistant-request":
            call_id = _call_id(message)
            if not call_id:
                return jsonify({"error": "Unable to establish a secure call session."})
            session = controller.start(call_id, _caller_number(message))
            renewal.start(call_id)
            candidate_first_name = ""
            if session.state == "candidate_unique" and session.selected_member_id:
                display_name = controller.directory.member(
                    session.selected_member_id
                ).display_name
                candidate_first_name = display_name.split(maxsplit=1)[0]
            return jsonify(
                {
                    "assistant": _assistant_config(
                        session.state, candidate_first_name=candidate_first_name
                    )
                }
            )

        if message_type == "tool-calls":
            return jsonify(_handle_tool_calls(controller, renewal, message))

        if message_type == "status-update" and message.get("status") == "ended":
            call_id = _call_id(message)
            if call_id:
                controller.end(call_id)
                renewal.end(call_id)
        elif message_type == "end-of-call-report":
            call_id = _call_id(message)
            if call_id:
                controller.end(call_id)
                renewal.end(call_id)
        return jsonify({"ok": True})

    return app


def _assistant_config(
    candidate_state: str, *, candidate_first_name: str = ""
) -> Mapping[str, Any]:
    needs_name = candidate_state != "candidate_unique"
    candidate_label = candidate_first_name or "the member"
    first_message = (
        "Hi, I'm Wedge's AI assistant. This call may be recorded for quality. Thanks "
        "for calling us back. Before I access any member details, please say the full "
        "name of the member you're calling about."
        if needs_name
        else "Hi, I'm Wedge's AI assistant. This call may be recorded for quality. "
        "Thanks for calling us back. "
        f"Is this {candidate_first_name or 'the member'}?"
    )
    state_instruction = (
        "No unique caller-number candidate exists. Ask for the synthetic member's full "
        "name, then call select_member with exactly what the caller says. When a unique "
        "synthetic candidate is selected, call confirm_synthetic_candidate."
        if needs_name
        else "The greeting named the synthetic candidate's first name. That is a demo-only "
        "candidate check. If the caller confirms, immediately call "
        "confirm_synthetic_candidate. If they deny being that person, follow the "
        "Someone-else-answered branch below and disclose no member details."
    )
    system_prompt = f"""
You are the Wedge secure inbound member-support demo for Universal Community Health
Center. You help with Medi-Cal renewal questions. {state_instruction}

Hard privacy boundary:
- Before VERIFIED_STATE_ACTIVE, never say, confirm, hint at, or spell a surname,
  renewal status, or account detail. The synthetic first-name greeting below is the only
  exception.
- Never treat caller ID, a claimed name, or family relationship as identity verification.
- Never invent a member or account detail. Tool results are the only source of member data.
- This fictional demo intentionally has no keypad or second identity factor. Never imply
  that verbal candidate confirmation would be sufficient for real patient information.
- Before verification, you may identify Wedge/Universal Community Health Center and say
  only that the call concerns general coverage-renewal support. The initial synthetic
  first-name candidate greeting is the sole name exception. Do not say the surname or
  imply that the candidate has Medi-Cal, a pending renewal, or a due date.
- After verification, you may share exact fields from the synthetic member context when
  the caller requests them, including the synthetic mailing address. Label the record as
  synthetic when relevant, never infer a missing field, and do not read every field
  unprompted.

Verification workflow:
- If select_member reports no match, explain generically that no account was accessed
  and end politely. Do not list possible names.
- When the caller confirms the synthetic candidate, call confirm_synthetic_candidate
  exactly once. VERIFIED_STATE_ACTIVE is sticky for the rest of the call. Once received,
  never ask for identity again or claim another verification step is needed.
- Only after VERIFIED_STATE_ACTIVE, greet the member and use the returned synthetic
  renewal context.
- If a verified caller asks what information is available, immediately call
  get_verified_member_context. Answer directly from that result; do not fall back to a
  privacy warning or another verification request.

Someone-else-answered branch:
- If the person says they are not {candidate_label}, do not call
  confirm_synthetic_candidate and do
  not mention Medi-Cal, renewal, clinic, coverage, a packet, or any account detail.
- Classify the situation and call route_unverified_caller before responding. Follow its
  SAFE_RESPONSE and action exactly; do not improvise an account explanation.
- Use `member_available` when {candidate_label} can come to the phone; after the handoff,
  repeat the recording notice, ask "Is this {candidate_label}?", and continue only after
  the member confirms.
- Use `member_unavailable` when {candidate_label} cannot come to the phone. Do not request permission
  to call, collect a callback time, or leave a detailed message.
- Use `wrong_number` when {candidate_label} does not use the number or the caller says the number is
  wrong. Do not keep asking for {candidate_label}.
- Use `representative_claim` for a spouse, relative, caregiver, guardian, or claimed
  representative. A relationship or authorization claim alone never unlocks the record.
- Use `member_present_with_helper` if {candidate_label} is present but another person is
  helping or interpreting. Address the member directly and require the member's own confirmation; do not treat
  the helper as the member or independently authorized.
- Use `refuses_identity` when the person will not confirm who they are, `unclear_identity`
  when audio or the answer is ambiguous, `purpose_requested` when a third party asks why
  you are calling, and `member_deceased` for a death notification.
- A do-not-call request from any person takes priority: call record_contact_preference
  with `do_not_call` immediately. Do not demand identity before honoring it.
- Do not ask the other person's name or relationship unless needed to understand a direct
  question. A spouse, child, parent, caregiver, or person claiming to be an authorized
  representative is not automatically authorized. Explain that representative access
  requires a separate approved verification path that this demo does not provide.
- If they say it is a wrong number, apologize once, disclose nothing, do not ask for
  {candidate_label}, and close politely.

Server-directed turn control:
- The server, not you, controls the review sequence. Whenever a workflow tool returns
  NEXT_STEP, follow that step and speak its `say` value as the only question in the turn.
- Every turn may contain at most one question. The question must be the final sentence.
  Once you ask it, stop speaking completely and wait for a new caller utterance. Never
  answer the question yourself, preview the next step, or continue because of silence.
- When the caller answers the current review question, call the appropriate recording
  tool before speaking. Do not choose, combine, reorder, or preview later questions.
- You may briefly acknowledge an answer before the NEXT_STEP question, but keep the whole
  turn under 25 words. Never add a generic help offer.
- If speech is visibly incomplete, such as "No, I...", do not record it or repeat the
  question. Say "Go ahead—I'm listening," then combine the next fragment with it.
- For the clinic question, use exactly `keep_clinic`, `change_clinic`, `uncertain`, or
  `declined`. If the caller names a replacement clinic in the same answer, include that
  name in `proposed_clinic`; that explicit change request is sufficient consent for this
  fictional record. Do not ask them to state it again or ask for a redundant yes. After
  the tool confirms the update, acknowledge the clinic once and immediately ask the
  returned NEXT_STEP renewal question.

Automatic renewal review after verification:
1. Start the renewal review automatically. Explain once that you will ask a few short
   questions about coverage, clinic, the renewal packet, and contact information so
   important changes are not missed. Then ask whether their coverage is still active or
   whether they are changing coverage. Do not begin with "How can I help?" Do not merely
   say that you are "checking in" without explaining the review.
2. Use record_workflow_fact for every review answer. Use record_renewal_status only when
   the caller explicitly says the renewal is already completed. Acknowledge the answer in
   a few words, then ask exactly one next unanswered question.
3. Ask whether the current primary-care clinic returned in the verified member context is
   still working well or whether they want to change clinics. Never substitute a
   hard-coded clinic name for the current record value.
4. State that the synthetic record shows a yellow-envelope renewal packet was mailed.
   Ask whether it arrived. For that answer, always call record_workflow_fact with
   field=`packet_received` and value=`yes`, `no`, or `uncertain`. Once recorded, never ask
   about receiving the packet again. The server will separately return the help question.
5. Ask whether they moved or have any changes to their mailing address, phone number, or
   other contact information. If yes, follow the verified synthetic profile update rules.
6. If they want renewal help, offer free phone help or clinic help. If phone help is
   selected, ask once whether they want to request a callback, then follow the explicit
   callback branch. Never push or re-offer after no.
7. At the end, give a one-sentence summary of only their answers and selected next step,
   then say they are all done and can hang up. For ordinary completion, do not use the
   endCall tool; remain silent and let the caller hang up. Use endCall only for an explicit
   do-not-call request or when the caller directly asks you to end the call.
8. Maintain a mental set of questions, explanations, and offers already given. Never
   repeat one unless the caller explicitly asks you to repeat or clearly did not hear it.
   If the caller asks a question or interrupts, answer directly, then continue with only
   the next unanswered review question unless they decline the review.
- "No thanks," "stop," "I don't want to do this," or equivalent ends the review without
  persuasion. "Already renewed" records completed, skips packet/help questions, and does
  not restart them; ask only whether contact or clinic information changed, then close.

Verified synthetic profile updates:
- The editable fields are mailing address, contact phone, primary-care clinic,
  primary-care preference, preferred language, and secondary language.
- When the caller wants a change, ask for the new value. Read back the field and exact
  proposed value once, then ask for a clear confirmation. Only after a clear yes, call
  update_verified_member_information with caller_confirmed=true.
- Update one field at a time. Never infer a clinic, address, phone number, or language;
  never claim success before the tool returns UPDATED_SYNTHETIC_MEMBER.
- If the caller wants to change clinics but has not selected a replacement, update only
  primary_care_preference to say that a change is wanted and no replacement is selected.
- These writes affect only the persistent fictional demo record. They do not update
  Medi-Cal, a county, an EHR, a clinic, or the phone number used for caller lookup. State
  that limitation once when confirming the first update.

Explicit callback branch:
- Enter this branch only if the caller asks to be called. Immediately call
  record_contact_preference with their exact words to validate consent. A clear yes to
  the immediately preceding direct callback question counts as explicit consent; include
  that exact agent question in agent_prompt.
- Once consent is recorded, collect only missing callback details, one at a time: purpose,
  requested day/time window, timezone only if needed to interpret the time, and preferred
  language. The channel is phone. Reuse information already stated.
- Call record_contact_preference again with the structured details, then read back the
  request once. Say accurately that this synthetic demo records the request but does not
  place it in a real scheduling system or guarantee a call. Then follow NEXT_STEP and do
  not ask about callback preferences or timing again.
- If the caller changes, limits, revokes, or declines the request at any point, record the
  new preference immediately. Never continue collecting callback details after refusal.
  A decline or revocation permanently closes the callback branch for that call: acknowledge
  it once, follow NEXT_STEP, and never rephrase, revisit, or re-offer the callback.

Listening and consent rules:
- Treat "no", "no thanks", "I'm good", and equivalent Spanish responses as complete
  answers. Acknowledge once and move on or close. Do not sell, persuade, or ask the same
  question in different words.
- If the caller says the renewal is completed, immediately call record_renewal_status
  with completed, acknowledge it, and never resume packet or renewal-help questions.
  Finish only unanswered clinic and contact-information checks, then close.
- If the caller is busy, give a one-sentence purpose if useful and let them end. Being
  busy is not callback consent. Because this is an inbound call, suggest they call back
  when convenient instead of asking to contact them.
- Offer a callback at most once and only after the caller selects phone help. Otherwise,
  never offer or promise one unless the caller explicitly asks. Before acknowledging any
  callback, call record_contact_preference using the caller's exact words. If the tool
  blocks it, do not mention or schedule a callback.
- A callback condition stays conditional. A declined or revoked callback remains
  declined. A do-not-call request is acknowledged immediately, recorded, and followed
  by a brief apology and the endCall tool. Do not add renewal reminders afterward.
- If a family member, guardian, or other third party appears, do not assume authority or
  share context. Explain that authorization needs a separate approved verification path.

Polish and recovery:
- Tool calls are silent. Never say "hold on," "one moment," "one second," or any other
  waiting filler before or after a tool call. Call the tool immediately, then speak only
  the acknowledgement and NEXT_STEP returned by the server.
- Use natural contractions and varied acknowledgements. Do not overuse the member's name
  or say "great" in response to illness, death, frustration, or a refusal.
- Keep ordinary turns under two sentences and roughly 25 words unless the caller asks
  for detail. Never deliver the old multi-sentence yellow-envelope monologue.
- On interruption, stop the prior path and answer the interruption directly. Do not
  replay prior questions. Resume with only the next unanswered review question unless
  the caller declined the review.
- Never talk over caller speech. If the caller begins or resumes speaking, stop, listen
  through their complete thought, and respond only after the turn-taking delay.
- Do not use a generic closing or generic help offer as a fallback. If a request is out
  of scope, state the boundary once and name the closest available action once.
- When speech is ambiguous or noisy, reflect the short phrase you heard and ask one
  confirmation question. Do not guess or silently advance.
- If corrected, briefly state the corrected understanding and use it going forward.
- Reply in the language the caller is currently using; switch immediately when they
  switch. Preserve workflow state across the switch.
- If asked whether you are human, clearly say you are an AI assistant.
- Be clear that member data and actions are synthetic. Never give medical, eligibility,
  legal, or county-policy advice beyond the supplied context.
""".strip()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "route_unverified_caller",
                "description": (
                    "Returns the exact privacy-safe response for a non-member, ambiguous, "
                    "or third-party caller before the synthetic candidate confirms."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scenario": {
                            "type": "string",
                            "enum": [
                                "member_available",
                                "member_unavailable",
                                "wrong_number",
                                "representative_claim",
                                "member_present_with_helper",
                                "refuses_identity",
                                "unclear_identity",
                                "purpose_requested",
                                "member_deceased",
                            ],
                        },
                        "caller_words": {
                            "type": "string",
                            "description": "The person's exact words indicating the route.",
                        },
                    },
                    "required": ["scenario", "caller_words"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "select_member",
                "description": (
                    "Selects a synthetic member candidate from the caller's exact claimed "
                    "full name. This does not verify identity or release member details."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claimed_name": {
                            "type": "string",
                            "description": "The exact full name spoken by the caller.",
                        }
                    },
                    "required": ["claimed_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "confirm_synthetic_candidate",
                "description": (
                    "Unlocks the fictional demo record after the caller verbally confirms "
                    "the selected synthetic candidate. This is deliberately not a real "
                    "identity-verification method. Call exactly once."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_verified_member_context",
                "description": (
                    "Returns the permitted synthetic member context and authoritative "
                    "current verification state. Use whenever a caller asks what member "
                    "information is available or the model is uncertain whether the caller "
                    "has already verified. Never ask the caller to verify again first."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_verified_member_information",
                "description": (
                    "Persists one caller-confirmed change to the verified fictional demo "
                    "record. It never updates a real healthcare, county, or caller-ID "
                    "system. Call only after reading back the exact value and receiving "
                    "clear confirmation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": [
                                "mailing_address",
                                "contact_phone",
                                "primary_care_clinic",
                                "primary_care_preference",
                                "preferred_language",
                                "secondary_language",
                            ],
                        },
                        "new_value": {"type": "string"},
                        "caller_confirmed": {"type": "boolean"},
                    },
                    "required": ["field", "new_value", "caller_confirmed"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_renewal_status",
                "description": (
                    "Records that the caller explicitly says the renewal is already "
                    "completed. Never use this for a yellow-envelope packet answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["completed"],
                        }
                    },
                    "required": ["status"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_workflow_fact",
                "description": (
                    "Records a non-sensitive workflow answer after the caller states it. "
                    "Do not call merely to fill a checklist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "enum": [
                                "medi_cal_status",
                                "pcp_preference",
                                "moved_recently",
                                "contact_information_changed",
                                "packet_received",
                                "help_preference",
                                "language",
                            ],
                        },
                        "value": {
                            "type": "string",
                            "enum": [
                                "active",
                                "inactive",
                                "changing",
                                "uncertain",
                                "declined",
                                "keep_clinic",
                                "change_clinic",
                                "yes",
                                "no",
                                "phone",
                                "clinic",
                                "self_service",
                                "none",
                                "english",
                                "spanish",
                                "other",
                            ],
                            "description": (
                                "Use active/inactive/changing for medi_cal_status; "
                                "keep_clinic/change_clinic for pcp_preference; yes/no "
                                "for packet or contact change; phone/clinic/self_service/"
                                "none for help_preference. Use uncertain or declined "
                                "when appropriate."
                            ),
                        },
                        "proposed_clinic": {
                            "type": "string",
                            "description": (
                                "Replacement clinic named by the caller in this answer; "
                                "only for pcp_preference=change_clinic."
                            ),
                        },
                    },
                    "required": ["field", "value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_contact_preference",
                "description": (
                    "Validates and records explicit callback consent, refusal, revocation, "
                    "conditional consent, or a do-not-call request."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "preference": {
                            "type": "string",
                            "enum": [
                                "callback_requested",
                                "conditional_callback",
                                "no_callback",
                                "callback_revoked",
                                "do_not_call",
                            ],
                        },
                        "caller_words": {
                            "type": "string",
                            "description": "The caller's exact words about future contact.",
                        },
                        "requested_time": {"type": "string"},
                        "condition": {"type": "string"},
                        "channel": {"type": "string"},
                        "purpose": {"type": "string"},
                        "timezone": {"type": "string"},
                        "language": {"type": "string"},
                        "agent_prompt": {
                            "type": "string",
                            "description": (
                                "The exact immediately preceding assistant question, used "
                                "only when the caller answers yes to a direct callback question."
                            ),
                        },
                    },
                    "required": ["preference", "caller_words"],
                },
            },
        },
        {"type": "endCall"},
    ]
    for tool in tools:
        # An omitted/empty request-start configuration makes Vapi inject default spoken
        # fillers ("Hold on a sec", "One moment", etc.). An explicit empty-content
        # request-start is Vapi's silent override.
        tool["messages"] = [{"type": "request-start", "content": ""}]
    return {
        "name": "Wedge Secure Inbound Demo",
        "firstMessage": first_message,
        "firstMessageInterruptionsEnabled": False,
        "model": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "temperature": 0.1,
            "messages": [{"role": "system", "content": system_prompt}],
            "tools": tools,
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "cgSgspJ2msm6clMCkdW9",
            "speed": 0.92,
        },
        "startSpeakingPlan": {
            "waitSeconds": 1.2,
            "smartEndpointingPlan": {"provider": "vapi"},
        },
        "stopSpeakingPlan": {
            "numWords": 1,
            "backoffSeconds": 1.5,
        },
        # Vapi's default silence timeout ended a real demo while the agent was waiting
        # for a clinic confirmation. A normal thinking pause must not terminate a call.
        "silenceTimeoutSeconds": 300,
        "maxDurationSeconds": 900,
        "endCallMessage": "Thank you for testing the secure Wedge demo. Goodbye.",
    }


def _handle_tool_calls(
    controller: IdentityController,
    workflow: RenewalWorkflowController,
    message: Mapping[str, Any],
) -> Mapping[str, Any]:
    call_id = _call_id(message)
    raw_calls = message.get("toolCallList") or []
    results: list[Mapping[str, str]] = []
    if not call_id or not isinstance(raw_calls, list):
        return {"results": results}
    for raw_call in raw_calls:
        if not isinstance(raw_call, Mapping):
            continue
        tool_id = str(raw_call.get("id") or "")
        function = raw_call.get("function")
        if isinstance(function, Mapping):
            name = str(function.get("name") or "")
            arguments = function.get("arguments") or {}
        else:
            name = str(raw_call.get("name") or "")
            arguments = raw_call.get("parameters") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, Mapping):
            arguments = {}
        try:
            _validate_tool_turn(workflow, call_id, name, arguments, message)
            result = _run_tool(controller, workflow, call_id, name, arguments)
            results.append({"toolCallId": tool_id, "result": result})
        except (IdentityError, PermissionError, WorkflowError) as error:
            detail = str(error)
            if detail == "NO_NEW_CALLER_TURN":
                next_step = workflow.next_step(call_id)
                detail = (
                    "NO_NEW_CALLER_TURN: this tool call reused the caller's previous "
                    "utterance. Do not infer an answer or call another state-changing "
                    "tool. Speak NEXT_STEP exactly, then stop and wait for a new caller "
                    "utterance: "
                    + json.dumps(dict(next_step), separators=(",", ":"))
                )
            results.append({"toolCallId": tool_id, "error": detail})
    return {"results": results}


def _validate_tool_turn(
    workflow: RenewalWorkflowController,
    call_id: str,
    name: str,
    arguments: Mapping[str, Any],
    message: Mapping[str, Any],
) -> None:
    state_changing = {
        "route_unverified_caller",
        "confirm_synthetic_candidate",
        "get_verified_member_context",
        "update_verified_member_information",
        "record_renewal_status",
        "record_workflow_fact",
        "record_contact_preference",
    }
    if name not in state_changing:
        return

    caller_fingerprint, caller_text = _latest_caller_turn(message)
    if name == "record_workflow_fact":
        step = str(workflow.next_step(call_id).get("step") or "")
        expected_fields = {
            "coverage": "medi_cal_status",
            "clinic": "pcp_preference",
            "packet": "packet_received",
            "renewal_help": "help_preference",
            "contact_changes": "contact_information_changed",
        }
        expected = expected_fields.get(step)
        actual = str(arguments.get("field") or "")
        if not expected or actual != expected:
            raise WorkflowError(
                f"OUT_OF_SEQUENCE_TOOL: current step is {step!r}; expected field "
                f"{expected!r}, not {actual!r}. Do not record or infer an answer."
            )
        if actual == "help_preference":
            normalized = " ".join(caller_text.casefold().split())
            if normalized in {"yes", "yes.", "yeah", "yeah.", "sure", "sure."}:
                raise WorkflowError(
                    "AMBIGUOUS_HELP_CHOICE: a bare affirmation does not select phone, "
                    "clinic, self-service, or no help. Ask the help options once and wait."
                )

    workflow.consume_caller_turn(call_id, caller_fingerprint)


def _latest_caller_turn(message: Mapping[str, Any]) -> tuple[str, str]:
    artifact = message.get("artifact")
    messages = artifact.get("messages") if isinstance(artifact, Mapping) else None
    if not isinstance(messages, list):
        return "", ""
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        text = str(
            item.get("message") or item.get("content") or item.get("transcript") or ""
        )
        timestamp = item.get("time")
        if timestamp is None:
            timestamp = item.get("secondsFromStart")
        return f"{timestamp!r}:{index}:{text}", text
    return "", ""


def _run_tool(
    controller: IdentityController,
    workflow: RenewalWorkflowController,
    call_id: str,
    name: str,
    arguments: Mapping[str, Any],
) -> str:
    if name == "route_unverified_caller":
        scenario = str(arguments.get("scenario") or "")
        identity_session = controller.session(call_id)
        candidate_label = "the member"
        if identity_session.selected_member_id:
            candidate_label = controller.directory.member(
                identity_session.selected_member_id
            ).display_name.split(maxsplit=1)[0]
        routes = {
            "member_available": {
                "action": "wait_for_handoff",
                "say": (
                    f"Thanks for letting me know. Please put {candidate_label} "
                    "on the phone."
                ),
                "after": (
                    "When the next person speaks, say: 'Hi, I'm Wedge's AI assistant. "
                    f"This call may be recorded for quality. Is this {candidate_label}?'"
                ),
            },
            "member_unavailable": {
                "action": "close_without_details",
                "say": (
                    f"No problem. Please ask {candidate_label} to call us back at this number. "
                    "I can't discuss any details with someone else."
                ),
            },
            "wrong_number": {
                "action": "close_wrong_number",
                "say": (
                    "I'm sorry for the mix-up. I won't discuss any details. "
                    "Thank you for letting me know."
                ),
            },
            "representative_claim": {
                "action": "explain_representative_boundary",
                "say": (
                    f"I can't discuss {candidate_label}'s information based only on a relationship "
                    "or authorization claim. Representative access requires a separate "
                    "approved verification process."
                ),
            },
            "member_present_with_helper": {
                "action": "address_member_directly",
                "say": (
                    f"That's fine. I need {candidate_label} to answer directly first. "
                    f"{candidate_label.capitalize()}, is this you?"
                ),
            },
            "refuses_identity": {
                "action": "close_identity_refused",
                "say": (
                    "That's okay. I can't discuss account details without confirming "
                    f"the member. {candidate_label.capitalize()} can call us back when convenient."
                ),
            },
            "unclear_identity": {
                "action": "clarify_once",
                "say": f"Sorry, I didn't catch that. Are you {candidate_label}?",
            },
            "purpose_requested": {
                "action": "generic_purpose_only",
                "say": (
                    "This is Wedge member support returning a call. I need to speak "
                    f"with {candidate_label} before discussing the reason."
                ),
            },
            "member_deceased": {
                "action": "empathetic_close",
                "say": (
                    "I'm very sorry. I won't continue with this account discussion. "
                    "Please contact member services through the official number if "
                    "you need help with next steps."
                ),
            },
        }
        route = routes.get(scenario)
        if not route:
            raise IdentityError("Unsupported unverified caller route")
        return (
            "UNVERIFIED_ROUTE. Disclose no Medi-Cal, renewal, clinic, coverage, packet, "
            "or account information. Follow this route exactly: "
            + json.dumps(route, separators=(",", ":"))
        )
    if name == "select_member":
        selected = controller.claim_name(call_id, str(arguments.get("claimed_name") or ""))
        if selected:
            return (
                "SYNTHETIC CANDIDATE SELECTED. Call confirm_synthetic_candidate now. "
                "Do not add another identity question."
            )
        return (
            "NO UNIQUE MATCH. No account information was accessed. Do not list or "
            "confirm possible member names."
        )
    if name == "confirm_synthetic_candidate":
        existing = controller.session(call_id)
        already_verified = existing.state == "verified_member"
        session = controller.confirm_synthetic_candidate(call_id)
        if not session.capability_token:
            raise IdentityError("Synthetic candidate confirmation failed")
        context = controller.member_context(call_id, session.capability_token)
        workflow.set_current_clinic(
            call_id,
            str(context.get("primary_care_clinic") or "Universal Community Health Center"),
        )
        next_step = workflow.next_step(call_id)
        return (
            (
                "VERIFIED_STATE_ACTIVE_ALREADY_CONFIRMED. Do not mention identity again. "
                if already_verified
                else "VERIFIED_STATE_ACTIVE. Synthetic candidate confirmed. "
            )
            + "This is a fictional demo record, not real identity verification. You may "
            "now use only this synthetic member context: "
            + json.dumps(dict(context), separators=(",", ":"))
            + ". Before NEXT_STEP, say exactly: 'We’d like to discuss your Medi-Cal "
            "renewal review. Renewing on time can help prevent a gap in coverage. I’ll "
            "ask a few short questions about your coverage, clinic, renewal packet, and "
            "contact information.' Then follow NEXT_STEP exactly: "
            + json.dumps(dict(next_step), separators=(",", ":"))
        )
    if name == "get_verified_member_context":
        session = controller.session(call_id)
        if session.state != "verified_member" or not session.capability_token:
            raise PermissionError("Member context requires completed synthetic confirmation")
        context = controller.member_context(call_id, session.capability_token)
        return (
            "VERIFIED_STATE_ACTIVE. Verification is already complete and must not be "
            "requested or discussed again. Answer the caller's current question directly "
            "using only this synthetic member context: "
            + json.dumps(dict(context), separators=(",", ":"))
            + ". Do not repeat a generic help question."
        )
    if name == "update_verified_member_information":
        session = controller.session(call_id)
        if not session.capability_token:
            raise PermissionError("Member updates require verified identity")
        field_name = str(arguments.get("field") or "")
        new_value = str(arguments.get("new_value") or "")
        controller.update_member_context(
            call_id,
            session.capability_token,
            field_name,
            new_value,
            caller_confirmed=arguments.get("caller_confirmed") is True,
        )
        workflow.record_profile_update(call_id, field_name)
        if field_name == "primary_care_clinic":
            workflow.set_current_clinic(call_id, new_value)
        next_step = workflow.next_step(call_id)
        return (
            "UPDATED_SYNTHETIC_MEMBER: "
            + json.dumps(
                {"field": field_name, "new_value": " ".join(new_value.split())},
                separators=(",", ":"),
            )
            + ". Confirm the fictional record update briefly. Do not say a real clinic, "
            "county, EHR, Medi-Cal, or caller-ID system was updated, and do not ask for "
            "this field again. Follow NEXT_STEP exactly: "
            + json.dumps(dict(next_step), separators=(",", ":"))
        )
    if name == "record_renewal_status":
        status = str(arguments.get("status") or "")
        workflow.record_renewal_status(call_id, status)
        next_step = workflow.next_step(call_id)
        if status == "completed":
            return (
                "RECORDED: renewal completed. Acknowledge this and never resume packet "
                "or renewal-help questions. Follow NEXT_STEP exactly: "
                + json.dumps(dict(next_step), separators=(",", ":"))
            )
        if status == "declined_help":
            return (
                "RECORDED: help declined. Follow NEXT_STEP exactly: "
                + json.dumps(dict(next_step), separators=(",", ":"))
            )
        return (
            f"RECORDED: renewal status is {status}. Follow NEXT_STEP exactly: "
            + json.dumps(dict(next_step), separators=(",", ":"))
        )
    if name == "record_workflow_fact":
        field_name = str(arguments.get("field") or "")
        value = str(arguments.get("value") or "")
        workflow.record_fact(call_id, field_name, value)
        proposed_clinic = " ".join(
            str(arguments.get("proposed_clinic") or "").split()
        )[:120]
        if (
            field_name == "pcp_preference"
            and value == "change_clinic"
            and proposed_clinic
        ):
            session = controller.session(call_id)
            if not session.capability_token:
                raise PermissionError("Clinic updates require verified identity")
            controller.update_member_context(
                call_id,
                session.capability_token,
                "primary_care_clinic",
                proposed_clinic,
                caller_confirmed=True,
            )
            workflow.record_profile_update(call_id, "primary_care_clinic")
            workflow.set_current_clinic(call_id, proposed_clinic)
            next_step = workflow.next_step(call_id)
            return (
                "UPDATED_SYNTHETIC_MEMBER: "
                + json.dumps(
                    {
                        "field": "primary_care_clinic",
                        "new_value": proposed_clinic,
                    },
                    separators=(",", ":"),
                )
                + ". Briefly acknowledge that the fictional clinic is now "
                + proposed_clinic
                + ". Do not ask for confirmation or repeat the clinic question. "
                "Immediately follow NEXT_STEP exactly: "
                + json.dumps(dict(next_step), separators=(",", ":"))
            )
        else:
            next_step = workflow.next_step(call_id)
        return (
            f"RECORDED: {field_name}={value}. Do not ask for it again. "
            "Follow NEXT_STEP exactly: "
            + json.dumps(dict(next_step), separators=(",", ":"))
        )
    if name == "record_contact_preference":
        decision = workflow.record_contact_preference(
            call_id,
            str(arguments.get("preference") or ""),
            str(arguments.get("caller_words") or ""),
            requested_time=str(arguments.get("requested_time") or ""),
            condition=str(arguments.get("condition") or ""),
            channel=str(arguments.get("channel") or ""),
            purpose=str(arguments.get("purpose") or ""),
            timezone=str(arguments.get("timezone") or ""),
            language=str(arguments.get("language") or ""),
            agent_prompt=str(arguments.get("agent_prompt") or ""),
        )
        if decision.get("decision") == "blocked":
            session = workflow.session(call_id)
            next_step = workflow.next_step(call_id)
            if session.contact_preference in {
                "no_callback",
                "callback_revoked",
                "do_not_call",
            }:
                return (
                    "BLOCKED_BY_REFUSAL: The callback branch is permanently closed for "
                    "this call. Do not mention, offer, schedule, or collect details for "
                    "a callback again. Follow NEXT_STEP exactly: "
                    + json.dumps(dict(next_step), separators=(",", ":"))
                )
            return (
                "BLOCKED: "
                + str(decision.get("reason") or "No explicit callback consent.")
                + " Do not offer, schedule, or promise a callback. Follow NEXT_STEP "
                "exactly: "
                + json.dumps(dict(next_step), separators=(",", ":"))
            )
        preference = str(decision.get("preference") or "")
        if preference == "do_not_call":
            return (
                "RECORDED: do not call. Apologize briefly, confirm no further outreach, "
                "do not add renewal messaging, and end the call."
            )
        if preference in {"no_callback", "callback_revoked"}:
            next_step = workflow.next_step(call_id)
            return (
                "RECORDED: callback declined or revoked. The callback branch is "
                "permanently closed for this call. Acknowledge once; do not mention, "
                "rephrase, revisit, or offer another callback. Follow NEXT_STEP exactly: "
                + json.dumps(dict(next_step), separators=(",", ":"))
            )
        if not decision.get("requested_time"):
            return (
                "RECORDED: explicit callback consent. Ask one short question for the "
                "caller's preferred day and time window, then record the structured "
                "request. Do not repeat the callback offer."
            )
        next_step = workflow.next_step(call_id)
        return (
            "RECORDED: structured callback request preserved as "
            + json.dumps(dict(decision), separators=(",", ":"))
            + ". This demo records the request but does not actually schedule outreach; "
            "state that accurately and read the request back only once. Do not ask for "
            "callback consent or timing again. Follow NEXT_STEP exactly: "
            + json.dumps(dict(next_step), separators=(",", ":"))
        )
    raise IdentityError("Unsupported tool call")


def _call_id(message: Mapping[str, Any]) -> str:
    call = message.get("call")
    return str(call.get("id") or "") if isinstance(call, Mapping) else ""


def _caller_number(message: Mapping[str, Any]) -> str:
    call = message.get("call")
    if isinstance(call, Mapping):
        source = call.get("from")
        if isinstance(source, Mapping) and source.get("phoneNumber"):
            return str(source["phoneNumber"])
        customer = call.get("customer")
        if isinstance(customer, Mapping) and customer.get("number"):
            return str(customer["number"])
    customer = message.get("customer")
    if isinstance(customer, Mapping) and customer.get("number"):
        return str(customer["number"])
    return ""


class VapiClient:
    def __init__(self, api_key: str, base_url: str = VAPI_API_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def create_number(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("POST", "/phone-number", payload)

    def update_number(self, number_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("PATCH", f"/phone-number/{number_id}", payload)

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        body = json.dumps(dict(payload)).encode()
        req = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "wedge-voice-qa/0.1",
            },
        )
        try:
            with urlopen(req, timeout=30) as response:
                value = json.loads(response.read().decode())
        except HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise IdentityError(f"Vapi API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise IdentityError(f"Unable to reach Vapi API: {error.reason}") from error
        if not isinstance(value, Mapping):
            raise IdentityError("Vapi API returned an unexpected response")
        return value


def phone_number_payload(
    *, public_base_url: str, webhook_secret: str, area_code: str | None = None
) -> Mapping[str, Any]:
    value: dict[str, Any] = {
        "server": {
            "url": public_base_url.rstrip("/") + "/vapi/webhook",
            "headers": {"Authorization": f"Bearer {webhook_secret}"},
        },
        "assistantId": None,
        "squadId": None,
    }
    if area_code:
        value.update({"provider": "vapi", "numberDesiredAreaCode": area_code})
    return value


def _state_path() -> Path:
    return Path(
        os.getenv(
            "VAPI_DEMO_STATE_PATH",
            str(project_root() / ".vapi-demo" / "vapi-number.json"),
        )
    )


def _saved_number_id() -> str:
    path = _state_path()
    if not path.exists():
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return str(value.get("phone_number_id") or "") if isinstance(value, Mapping) else ""


def synthetic_member_context(renewal_summary: str) -> Mapping[str, str]:
    """Return a rich, explicitly fictional record for voice-demo testing."""
    return {
        "record_type": "Synthetic demonstration record; not a real patient record.",
        "date_of_birth": "April 14, 1982",
        "mailing_address": (
            "1847 Cedar Avenue, Apartment 3B, Los Angeles, California 90007"
        ),
        "county": "Los Angeles County",
        "preferred_language": "English",
        "secondary_language": "Spanish",
        "contact_phone": "Synthetic phone number (213) 555-0142",
        "medi_cal_status": "Active in this synthetic record as of October 1, 2026",
        "primary_care_clinic": "Universal Community Health Center",
        "primary_care_preference": "Keep the current clinic",
        "renewal_summary": renewal_summary,
        "renewal_deadline": "November 15, 2026",
        "renewal_packet_status": "Mailed; delivery has not been confirmed",
        "renewal_packet_description": "County renewal packet in a yellow envelope",
        "recent_move_status": "No move is recorded",
        "authorized_contacts": "No authorized third party is listed",
        "support_options": "Free synthetic help is available by phone or at the clinic",
        "callback_status": (
            "No callback is currently requested; fresh explicit consent is required"
        ),
        "callback_phone": "Synthetic phone number ending in 0142",
        "callback_notice": (
            "The demo can record a callback request but does not schedule a real call"
        ),
        "packet_guidance": (
            "Follow the deadline printed on the packet and request help if needed"
        ),
    }


def _save_number(value: Mapping[str, Any]) -> None:
    number_id = str(value.get("id") or "")
    if not number_id:
        return
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"phone_number_id": number_id, "number": str(value.get("number") or "")},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-qa-vapi-demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed", help="add a synthetic member")
    seed.add_argument("--phone", required=True, help="approved caller number in E.164")
    seed.add_argument("--member-id", default="james-thornton")
    seed.add_argument("--name", default="James Thornton")
    seed.add_argument("--pin", help="four digits; omit to enter without shell history")
    seed.add_argument(
        "--renewal-summary",
        default=(
            "The synthetic annual renewal is pending; no completed response is recorded."
        ),
    )
    seed.add_argument("--data", type=Path)

    serve = subparsers.add_parser("serve", help="run the Vapi webhook server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--data", type=Path)

    setup = subparsers.add_parser("setup", help="create or configure a free inbound number")
    setup.add_argument("--area-code", default="425")
    setup.add_argument("--phone-number-id")
    return parser


def _data_path(value: Path | None) -> Path:
    return value or Path(
        os.getenv(
            "VAPI_DEMO_DATA_PATH",
            str(project_root() / ".vapi-demo" / "members.json"),
        )
    )


def run(argv: Sequence[str] | None = None) -> int:
    load_dotenv(".env.local")
    args = build_parser().parse_args(argv)
    pepper = os.getenv("VAPI_DEMO_PEPPER", "")
    if args.command == "setup":
        api_key = os.getenv("VAPI_API_KEY", "")
        public_base_url = os.getenv("VAPI_PUBLIC_BASE_URL", "").rstrip("/")
        webhook_secret = os.getenv("VAPI_WEBHOOK_SECRET", "")
        if not api_key:
            raise SystemExit("Set VAPI_API_KEY in .env.local")
        if not public_base_url.startswith("https://"):
            raise SystemExit("Set VAPI_PUBLIC_BASE_URL to the public HTTPS tunnel URL")
        if len(webhook_secret) < 24:
            raise SystemExit("Set VAPI_WEBHOOK_SECRET to a random value of at least 24 characters")
        client = VapiClient(api_key)
        number_id = (
            args.phone_number_id
            or os.getenv("VAPI_PHONE_NUMBER_ID", "")
            or _saved_number_id()
        )
        if number_id:
            result = client.update_number(
                number_id,
                phone_number_payload(
                    public_base_url=public_base_url,
                    webhook_secret=webhook_secret,
                ),
            )
            action = "Configured"
        else:
            result = client.create_number(
                phone_number_payload(
                    public_base_url=public_base_url,
                    webhook_secret=webhook_secret,
                    area_code=args.area_code,
                )
            )
            action = "Created"
        _save_number(result)
        print(f"{action} Vapi number {result.get('number', '(pending)')}")
        print(f"VAPI_PHONE_NUMBER_ID={result.get('id', number_id)}")
        return 0

    if len(pepper) < 32:
        raise SystemExit("Set VAPI_DEMO_PEPPER to a random value of at least 32 characters")
    if args.command == "seed":
        pin = args.pin or getpass.getpass("Four-digit synthetic demo PIN: ")
        member = seed_member(
            path=_data_path(args.data),
            pepper=pepper,
            member_id=args.member_id,
            display_name=args.name,
            phone=args.phone,
            pin=pin,
            safe_context={
                "organization": "Universal Community Health Center",
                "program": "Medi-Cal",
                **synthetic_member_context(args.renewal_summary),
            },
        )
        print(
            f"Seeded synthetic member {member.id} in {_data_path(args.data)}. "
            "Raw phone and PIN were not stored."
        )
        return 0

    webhook_secret = os.getenv("VAPI_WEBHOOK_SECRET", "")
    directory = DemoDirectory.from_file(_data_path(args.data), pepper)
    app = create_app(
        controller=IdentityController(directory), webhook_secret=webhook_secret
    )
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except IdentityError as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
