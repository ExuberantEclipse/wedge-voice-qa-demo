"""Twilio inbound phone demo for caller-number-assisted member identification."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

from .caller_identity import (
    DemoDirectory,
    IdentityController,
    IdentityError,
    seed_member,
)
from .config import project_root


def create_app(
    *,
    controller: IdentityController,
    auth_token: str,
    public_base_url: str,
    validate_signatures: bool = True,
) -> Flask:
    app = Flask(__name__)
    base_url = public_base_url.rstrip("/")
    validator = RequestValidator(auth_token)

    def absolute(path: str) -> str:
        return base_url + path

    def authorized() -> bool:
        if not validate_signatures:
            return True
        signature = request.headers.get("X-Twilio-Signature", "")
        url = absolute(request.path)
        if request.query_string:
            url += "?" + request.query_string.decode("ascii")
        return validator.validate(url, request.form.to_dict(flat=True), signature)

    def twiml(value: VoiceResponse, status: int = 200) -> Response:
        return Response(str(value), status=status, content_type="application/xml")

    def reject() -> Response:
        return Response("Invalid Twilio signature", status=403, content_type="text/plain")

    def ask_for_name(response: VoiceResponse) -> None:
        gather = Gather(
            input="speech",
            action=absolute("/twilio/select-member"),
            method="POST",
            action_on_empty_result=True,
            speech_timeout="auto",
            language="en-US",
        )
        gather.say(
            "Thanks for calling Wedge. Before I access any account information, "
            "please say the full name of the person you are calling about."
        )
        response.append(gather)

    def ask_for_pin(response: VoiceResponse, retry: bool = False) -> None:
        gather = Gather(
            input="dtmf",
            action=absolute("/twilio/verify"),
            method="POST",
            action_on_empty_result=True,
            num_digits=4,
            timeout=8,
        )
        prefix = "That code did not match. " if retry else ""
        gather.say(
            prefix
            + "To verify your identity, enter your four digit demo code using the keypad."
        )
        response.append(gather)

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"status": "ok", "demo": "caller-number-identification"})

    @app.post("/twilio/voice")
    def voice() -> Response:
        if not authorized():
            return reject()
        call_sid = request.form.get("CallSid", "")
        caller = request.form.get("From", "")
        if not call_sid:
            return Response("Missing CallSid", status=400)
        session = controller.start(call_sid, caller)
        response = VoiceResponse()
        response.say("This is a synthetic demonstration. Do not provide real patient information.")
        if session.state == "candidate_unique":
            ask_for_pin(response)
        else:
            ask_for_name(response)
        response.say("I did not receive a response. Goodbye.")
        response.hangup()
        return twiml(response)

    @app.post("/twilio/select-member")
    def select_member() -> Response:
        if not authorized():
            return reject()
        call_sid = request.form.get("CallSid", "")
        claimed_name = request.form.get("SpeechResult", "")
        response = VoiceResponse()
        try:
            selected = controller.claim_name(call_sid, claimed_name)
        except IdentityError:
            selected = False
        if selected:
            ask_for_pin(response)
            response.say("I did not receive a code. Goodbye.")
        else:
            response.say(
                "I could not safely identify one synthetic member from that response. "
                "No account information was accessed. Goodbye."
            )
        response.hangup()
        return twiml(response)

    @app.post("/twilio/verify")
    def verify() -> Response:
        if not authorized():
            return reject()
        call_sid = request.form.get("CallSid", "")
        digits = request.form.get("Digits", "")
        response = VoiceResponse()
        try:
            session = controller.verify_pin(call_sid, digits)
        except IdentityError:
            response.say("The verification session is unavailable. No account was accessed.")
            response.hangup()
            return twiml(response)
        if session.state == "verified_member" and session.capability_token:
            context = controller.member_context(call_sid, session.capability_token)
            response.say(
                f"Verification successful. Hello {context['display_name']}. "
                f"{context.get('renewal_summary', 'Your synthetic member context is now available.')}"
            )
            response.say("This concludes the secure caller identification demo. Goodbye.")
            response.hangup()
        elif session.state == "locked":
            response.say(
                "Verification is locked after too many attempts. No account information "
                "was accessed. Please contact staff through an approved channel. Goodbye."
            )
            response.hangup()
        else:
            ask_for_pin(response, retry=True)
            response.say("I did not receive a code. Goodbye.")
            response.hangup()
        return twiml(response)

    @app.post("/twilio/status")
    def status() -> Response:
        if not authorized():
            return reject()
        call_sid = request.form.get("CallSid", "")
        call_status = request.form.get("CallStatus", "")
        if call_sid and call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
            controller.end(call_sid)
        return Response(status=204)

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-qa-twilio-demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed", help="add a synthetic member without storing raw phone/PIN")
    seed.add_argument("--phone", required=True, help="approved test caller number in E.164")
    seed.add_argument("--member-id", default="james-thornton")
    seed.add_argument("--name", default="James Thornton")
    seed.add_argument("--pin", help="four digits; omit to enter without shell history")
    seed.add_argument(
        "--renewal-summary",
        default="Your synthetic renewal was received and no further action is currently needed.",
    )
    seed.add_argument("--data", type=Path)

    serve = subparsers.add_parser("serve", help="run the Twilio webhook server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--data", type=Path)
    return parser


def _data_path(value: Path | None) -> Path:
    return value or Path(
        os.getenv(
            "TWILIO_DEMO_DATA_PATH",
            str(project_root() / ".twilio-demo" / "members.json"),
        )
    )


def run(argv: Sequence[str] | None = None) -> int:
    load_dotenv(".env.local")
    args = build_parser().parse_args(argv)
    pepper = os.getenv("TWILIO_DEMO_PEPPER", "")
    if len(pepper) < 32:
        raise SystemExit("Set TWILIO_DEMO_PEPPER to a random value of at least 32 characters")
    if args.command == "seed":
        pin = args.pin or getpass.getpass("Four-digit synthetic demo PIN: ")
        member = seed_member(
            path=_data_path(args.data),
            pepper=pepper,
            member_id=args.member_id,
            display_name=args.name,
            phone=args.phone,
            pin=pin,
            safe_context={"renewal_summary": args.renewal_summary},
        )
        print(
            f"Seeded synthetic member {member.id} in {_data_path(args.data)}. "
            "Raw phone and PIN were not stored."
        )
        return 0

    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    public_base_url = os.getenv("TWILIO_PUBLIC_BASE_URL", "").rstrip("/")
    validate_signatures = os.getenv("TWILIO_VALIDATE_SIGNATURES", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    if validate_signatures and not auth_token:
        raise SystemExit("Set TWILIO_AUTH_TOKEN; signature validation cannot run without it")
    if not public_base_url.startswith("https://"):
        raise SystemExit("Set TWILIO_PUBLIC_BASE_URL to the public HTTPS tunnel or deployment URL")
    directory = DemoDirectory.from_file(_data_path(args.data), pepper)
    app = create_app(
        controller=IdentityController(directory),
        auth_token=auth_token,
        public_base_url=public_base_url,
        validate_signatures=validate_signatures,
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
