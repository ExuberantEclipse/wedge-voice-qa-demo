import tempfile
import unittest
from pathlib import Path

from voice_qa.caller_identity import DemoDirectory, IdentityController, seed_member
from voice_qa.renewal_workflow import RenewalWorkflowController
from voice_qa.vapi_demo import (
    create_app,
    phone_number_payload,
    synthetic_member_context,
)


PEPPER = "test-pepper-that-is-at-least-thirty-two-characters"
WEBHOOK_SECRET = "test-vapi-webhook-secret-long-enough"
AUTH = {"Authorization": f"Bearer {WEBHOOK_SECRET}"}


def assistant_request(call_id: str, caller: str):
    return {
        "message": {
            "type": "assistant-request",
            "call": {
                "id": call_id,
                "from": {"phoneNumber": caller},
            },
        }
    }


def tool_request(
    call_id: str,
    tool_id: str,
    name: str,
    arguments: dict,
    caller_turn: tuple[int, str] | None = None,
):
    message = {
        "type": "tool-calls",
        "call": {"id": call_id},
        "toolCallList": [
            {
                "id": tool_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }
    if caller_turn:
        timestamp, text = caller_turn
        message["artifact"] = {
            "messages": [{"role": "user", "time": timestamp, "message": text}]
        }
    return {"message": message}


class VapiDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "members.json"
        self.path = path
        seed_member(
            path=path,
            pepper=PEPPER,
            member_id="james-thornton",
            display_name="James Thornton",
            phone="+14255550123",
            pin="2468",
            safe_context=synthetic_member_context(
                "Your synthetic renewal was received."
            ),
        )
        self.controller = IdentityController(DemoDirectory.from_file(path, PEPPER))
        self.workflow = RenewalWorkflowController()
        app = create_app(
            controller=self.controller,
            webhook_secret=WEBHOOK_SECRET,
            workflow=self.workflow,
        )
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, payload):
        return self.client.post("/vapi/webhook", json=payload, headers=AUTH)

    def test_webhook_requires_configured_bearer_token(self):
        response = self.client.post(
            "/vapi/webhook", json=assistant_request("call-1", "+14255550123")
        )
        self.assertEqual(response.status_code, 401)

    def test_known_caller_gets_synthetic_first_name_without_keypad(self):
        response = self.post(assistant_request("call-1", "+14255550123"))
        self.assertEqual(response.status_code, 200)
        body = response.get_json()["assistant"]
        serialized = response.get_data(as_text=True)
        self.assertIn("Is this James", body["firstMessage"])
        self.assertLess(
            body["firstMessage"].index("may be recorded"),
            body["firstMessage"].index("Is this James"),
        )
        self.assertIn("Thanks for calling us back", body["firstMessage"])
        self.assertNotIn("synthetic demonstration", body["firstMessage"])
        self.assertNotIn("don't provide real patient information", body["firstMessage"])
        self.assertNotIn("Medi-Cal renewal", body["firstMessage"])
        self.assertNotIn("gap in coverage", body["firstMessage"])
        self.assertNotIn("Thornton", serialized)
        self.assertNotIn("renewal was received", serialized)
        self.assertNotIn("1847 Cedar", serialized)
        self.assertNotIn("keypadInputPlan", body)

    def test_unknown_caller_is_asked_for_name_without_listing_names(self):
        response = self.post(assistant_request("call-2", "+14255559999"))
        body = response.get_json()["assistant"]
        self.assertIn("say the full name", body["firstMessage"])
        self.assertNotIn("James", response.get_data(as_text=True))

    def test_member_context_is_released_after_synthetic_candidate_confirmation(self):
        self.post(assistant_request("call-3", "+14255550123"))
        correct = self.post(
            tool_request(
                "call-3", "tool-2", "confirm_synthetic_candidate", {}
            )
        )
        correct_result = correct.get_json()["results"][0]["result"]
        self.assertIn("VERIFIED_STATE_ACTIVE", correct_result)
        self.assertIn("discuss your Medi-Cal renewal review", correct_result)
        self.assertIn("prevent a gap in coverage", correct_result)
        self.assertIn("coverage, clinic, renewal packet", correct_result)
        self.assertIn("James Thornton", correct_result)
        self.assertIn("synthetic renewal was received", correct_result)
        self.assertIn("1847 Cedar Avenue", correct_result)
        self.assertIn('"step":"coverage"', correct_result)

        repeated = self.post(
            tool_request(
                "call-3", "tool-3", "confirm_synthetic_candidate", {}
            )
        )
        repeated_result = repeated.get_json()["results"][0]["result"]
        self.assertIn("VERIFIED_STATE_ACTIVE_ALREADY_CONFIRMED", repeated_result)
        self.assertIn("Do not mention identity again", repeated_result)

    def test_member_context_lookup_uses_sticky_verified_state(self):
        self.post(assistant_request("call-context", "+14255550123"))
        denied = self.post(
            tool_request(
                "call-context", "tool-context-1", "get_verified_member_context", {}
            )
        )
        self.assertIn(
            "completed synthetic confirmation",
            denied.get_json()["results"][0]["error"],
        )

        self.post(
            tool_request(
                "call-context", "tool-confirm", "confirm_synthetic_candidate", {}
            )
        )
        allowed = self.post(
            tool_request(
                "call-context", "tool-context-2", "get_verified_member_context", {}
            )
        )
        result = allowed.get_json()["results"][0]["result"]
        self.assertIn("VERIFIED_STATE_ACTIVE", result)
        self.assertIn("Verification is already complete", result)
        self.assertIn("James Thornton", result)

    def test_unknown_caller_selects_exact_name_then_verifies(self):
        self.post(assistant_request("call-4", "+14255559999"))
        selected = self.post(
            tool_request(
                "call-4",
                "tool-1",
                "select_member",
                {"claimed_name": "James Thornton"},
            )
        )
        result = selected.get_json()["results"][0]["result"]
        self.assertIn("CANDIDATE SELECTED", result)
        self.assertNotIn("James", result)

        verified = self.post(
            tool_request(
                "call-4", "tool-2", "confirm_synthetic_candidate", {}
            )
        )
        self.assertIn("VERIFIED", verified.get_json()["results"][0]["result"])

    def test_assistant_uses_inbound_dynamic_flow_and_end_call_tool(self):
        response = self.post(assistant_request("call-flow", "+14255550123"))
        assistant = response.get_json()["assistant"]
        prompt = assistant["model"]["messages"][0]["content"]
        tool_names = [
            tool.get("function", {}).get("name", tool.get("type"))
            for tool in assistant["model"]["tools"]
        ]
        self.assertIn("Start the renewal review automatically", prompt)
        self.assertEqual(assistant["model"]["model"], "gpt-4.1-mini")
        self.assertEqual(assistant["model"]["temperature"], 0.1)
        self.assertEqual(assistant["silenceTimeoutSeconds"], 300)
        self.assertEqual(assistant["maxDurationSeconds"], 900)
        self.assertFalse(assistant["firstMessageInterruptionsEnabled"])
        self.assertEqual(assistant["voice"]["speed"], 0.92)
        self.assertEqual(
            assistant["startSpeakingPlan"],
            {
                "waitSeconds": 1.2,
                "smartEndpointingPlan": {"provider": "vapi"},
            },
        )
        self.assertEqual(
            assistant["stopSpeakingPlan"],
            {"numWords": 1, "backoffSeconds": 1.5},
        )
        self.assertIn("busy is not callback consent", " ".join(prompt.split()))
        self.assertIn("Reply in the language the caller is currently using", prompt)
        self.assertIn("record_renewal_status", tool_names)
        self.assertIn("record_contact_preference", tool_names)
        self.assertIn("get_verified_member_context", tool_names)
        self.assertIn("confirm_synthetic_candidate", tool_names)
        self.assertIn("route_unverified_caller", tool_names)
        self.assertNotIn("verify_member", tool_names)
        self.assertIn("update_verified_member_information", tool_names)
        self.assertIn("endCall", tool_names)
        self.assertIn(
            "Never repeat one unless the caller explicitly asks",
            " ".join(prompt.split()),
        )
        self.assertIn("Automatic renewal review after verification", prompt)
        self.assertIn("Explicit callback branch", prompt)
        self.assertIn("Verified synthetic profile updates", prompt)
        self.assertIn("Server-directed turn control", prompt)
        self.assertIn("Every turn may contain at most one question", prompt)
        self.assertIn("Never talk over caller speech", prompt)
        self.assertIn("Someone-else-answered branch", prompt)
        self.assertIn("Use `member_available` when James can come to the phone", prompt)
        self.assertIn("not automatically authorized", prompt)
        self.assertIn("not mention Medi-Cal", prompt)
        function_tools = [
            tool for tool in assistant["model"]["tools"] if tool.get("type") == "function"
        ]
        silent_start = [{"type": "request-start", "content": ""}]
        self.assertTrue(
            all(tool.get("messages") == silent_start for tool in assistant["model"]["tools"])
        )
        fact_tool = next(
            tool
            for tool in function_tools
            if tool["function"]["name"] == "record_workflow_fact"
        )
        value_schema = fact_tool["function"]["parameters"]["properties"]["value"]
        self.assertIn("change_clinic", value_schema["enum"])
        self.assertNotIn("change clinics", value_schema["enum"])
        renewal_tool = next(
            tool
            for tool in function_tools
            if tool["function"]["name"] == "record_renewal_status"
        )
        renewal_schema = renewal_tool["function"]["parameters"]["properties"]["status"]
        self.assertEqual(renewal_schema["enum"], ["completed"])
        self.assertIn(
            "never ask about receiving the packet again", " ".join(prompt.split())
        )

    def test_recorded_answer_returns_one_server_directed_next_question(self):
        self.post(assistant_request("call-sequence", "+14255550123"))
        self.post(
            tool_request(
                "call-sequence",
                "tool-confirm-sequence",
                "confirm_synthetic_candidate",
                {},
            )
        )
        response = self.post(
            tool_request(
                "call-sequence",
                "tool-coverage",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
            )
        )
        result = response.get_json()["results"][0]["result"]
        self.assertIn('"step":"clinic"', result)
        self.assertIn("working well for you", result)
        self.assertNotIn("yellow-envelope", result)

    def test_one_state_transition_is_allowed_per_distinct_caller_turn(self):
        self.post(assistant_request("call-turn-guard", "+14255550123"))
        self.post(
            tool_request(
                "call-turn-guard",
                "tool-confirm-turn",
                "confirm_synthetic_candidate",
                {},
                caller_turn=(100, "Yes."),
            )
        )
        reused = self.post(
            tool_request(
                "call-turn-guard",
                "tool-reused-turn",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
                caller_turn=(100, "Yes."),
            )
        )
        reused_error = reused.get_json()["results"][0]["error"]
        self.assertIn("NO_NEW_CALLER_TURN", reused_error)
        self.assertIn('"step":"coverage"', reused_error)

        accepted = self.post(
            tool_request(
                "call-turn-guard",
                "tool-new-turn",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
                caller_turn=(200, "My coverage is active."),
            )
        )
        self.assertIn(
            '"step":"clinic"', accepted.get_json()["results"][0]["result"]
        )

        reused_again = self.post(
            tool_request(
                "call-turn-guard",
                "tool-reused-new-turn",
                "record_workflow_fact",
                {"field": "pcp_preference", "value": "keep_clinic"},
                caller_turn=(200, "My coverage is active."),
            )
        )
        self.assertIn(
            "NO_NEW_CALLER_TURN",
            reused_again.get_json()["results"][0]["error"],
        )

    def test_out_of_sequence_field_is_rejected_without_consuming_turn(self):
        self.post(assistant_request("call-sequence-guard", "+14255550123"))
        self.post(
            tool_request(
                "call-sequence-guard",
                "tool-confirm-sequence-guard",
                "confirm_synthetic_candidate",
                {},
            )
        )
        wrong = self.post(
            tool_request(
                "call-sequence-guard",
                "tool-wrong-field",
                "record_workflow_fact",
                {"field": "pcp_preference", "value": "keep_clinic"},
                caller_turn=(300, "Yes."),
            )
        )
        self.assertIn(
            "OUT_OF_SEQUENCE_TOOL", wrong.get_json()["results"][0]["error"]
        )
        corrected = self.post(
            tool_request(
                "call-sequence-guard",
                "tool-corrected-field",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
                caller_turn=(300, "Yes."),
            )
        )
        self.assertIn(
            '"step":"clinic"', corrected.get_json()["results"][0]["result"]
        )

    def test_bare_yes_cannot_select_phone_help(self):
        call_id = "call-help-guard"
        self.post(assistant_request(call_id, "+14255550123"))
        self.post(
            tool_request(
                call_id, "tool-confirm-help", "confirm_synthetic_candidate", {}
            )
        )
        for index, arguments in enumerate(
            [
                {"field": "medi_cal_status", "value": "active"},
                {"field": "pcp_preference", "value": "keep_clinic"},
                {"field": "packet_received", "value": "yes"},
            ]
        ):
            self.post(
                tool_request(
                    call_id,
                    f"tool-help-setup-{index}",
                    "record_workflow_fact",
                    arguments,
                )
            )
        ambiguous = self.post(
            tool_request(
                call_id,
                "tool-ambiguous-help",
                "record_workflow_fact",
                {"field": "help_preference", "value": "phone"},
                caller_turn=(400, "Yes."),
            )
        )
        self.assertIn(
            "AMBIGUOUS_HELP_CHOICE",
            ambiguous.get_json()["results"][0]["error"],
        )

    def test_unverified_caller_variations_return_safe_deterministic_routes(self):
        cases = {
            "member_available": ("put James on the phone", "wait_for_handoff"),
            "member_unavailable": ("ask James to call us back", "close_without_details"),
            "wrong_number": ("sorry for the mix-up", "close_wrong_number"),
            "representative_claim": (
                "separate approved verification process",
                "explain_representative_boundary",
            ),
            "member_present_with_helper": (
                "James to answer directly",
                "address_member_directly",
            ),
            "refuses_identity": ("without confirming the member", "close_identity_refused"),
            "unclear_identity": ("Are you James?", "clarify_once"),
            "purpose_requested": ("before discussing the reason", "generic_purpose_only"),
            "member_deceased": ("I'm very sorry", "empathetic_close"),
        }
        for index, (scenario, expected) in enumerate(cases.items()):
            with self.subTest(scenario=scenario):
                call_id = f"call-unverified-{index}"
                self.post(assistant_request(call_id, "+14255550123"))
                response = self.post(
                    tool_request(
                        call_id,
                        f"tool-route-{index}",
                        "route_unverified_caller",
                        {"scenario": scenario, "caller_words": "example response"},
                    )
                )
                result = response.get_json()["results"][0]["result"]
                self.assertIn("UNVERIFIED_ROUTE", result)
                self.assertIn(expected[0], result)
                self.assertIn(expected[1], result)
                self.assertNotIn("Universal Community Health Center", result)
                self.assertNotIn("yellow-envelope", result)

    def test_do_not_call_is_honored_before_member_confirmation(self):
        self.post(assistant_request("call-unverified-dnc", "+14255550123"))
        response = self.post(
            tool_request(
                "call-unverified-dnc",
                "tool-unverified-dnc",
                "record_contact_preference",
                {
                    "preference": "do_not_call",
                    "caller_words": "Stop calling this number",
                },
            )
        )
        result = response.get_json()["results"][0]["result"]
        self.assertIn("RECORDED: do not call", result)
        self.assertNotIn("verified", result.lower())

    def test_named_clinic_change_updates_and_resumes_renewal_without_reconfirmation(self):
        self.post(assistant_request("call-clinic", "+14255550123"))
        self.post(
            tool_request(
                "call-clinic", "tool-confirm", "confirm_synthetic_candidate", {}
            )
        )
        self.post(
            tool_request(
                "call-clinic",
                "tool-coverage",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
            )
        )
        response = self.post(
            tool_request(
                "call-clinic",
                "tool-clinic",
                "record_workflow_fact",
                {
                    "field": "pcp_preference",
                    "value": "change_clinic",
                    "proposed_clinic": "South Park Health Center",
                },
            )
        )
        result = response.get_json()["results"][0]["result"]
        self.assertIn("UPDATED_SYNTHETIC_MEMBER", result)
        self.assertIn("South Park Health Center", result)
        self.assertIn('"step":"packet"', result)
        self.assertIn("yellow-envelope", result)
        self.assertIn("Do not ask for confirmation", result)
        self.assertNotIn("still working well", result)
        self.assertEqual(
            self.controller.directory.member("james-thornton").safe_context[
                "primary_care_clinic"
            ],
            "South Park Health Center",
        )

        self.post(assistant_request("call-clinic-next", "+14255550123"))
        self.post(
            tool_request(
                "call-clinic-next",
                "tool-confirm-next",
                "confirm_synthetic_candidate",
                {},
            )
        )
        next_call = self.post(
            tool_request(
                "call-clinic-next",
                "tool-coverage-next",
                "record_workflow_fact",
                {"field": "medi_cal_status", "value": "active"},
            )
        )
        next_result = next_call.get_json()["results"][0]["result"]
        self.assertIn("South Park Health Center", next_result)
        self.assertNotIn("Universal Community Health Center", next_result)

    def test_synthetic_context_contains_demo_profile_and_callback_boundaries(self):
        context = synthetic_member_context("Synthetic renewal pending")
        self.assertEqual(
            context["mailing_address"],
            "1847 Cedar Avenue, Apartment 3B, Los Angeles, California 90007",
        )
        self.assertIn("November 15, 2026", context["renewal_deadline"])
        self.assertIn("No authorized third party", context["authorized_contacts"])
        self.assertIn("does not schedule a real call", context["callback_notice"])
        self.assertIn("213", context["contact_phone"])

    def test_verified_confirmed_profile_update_is_persisted_and_retrievable(self):
        self.post(assistant_request("call-update", "+14255550123"))
        denied = self.post(
            tool_request(
                "call-update",
                "tool-update-denied",
                "update_verified_member_information",
                {
                    "field": "mailing_address",
                    "new_value": "900 Synthetic Way, Los Angeles, California 90001",
                    "caller_confirmed": True,
                },
            )
        )
        self.assertIn("verified identity", denied.get_json()["results"][0]["error"])

        self.post(
            tool_request(
                "call-update", "tool-confirm", "confirm_synthetic_candidate", {}
            )
        )
        unconfirmed = self.post(
            tool_request(
                "call-update",
                "tool-update-unconfirmed",
                "update_verified_member_information",
                {
                    "field": "mailing_address",
                    "new_value": "900 Synthetic Way, Los Angeles, California 90001",
                    "caller_confirmed": False,
                },
            )
        )
        self.assertIn(
            "explicit caller confirmation",
            unconfirmed.get_json()["results"][0]["error"],
        )

        updated = self.post(
            tool_request(
                "call-update",
                "tool-update-confirmed",
                "update_verified_member_information",
                {
                    "field": "mailing_address",
                    "new_value": "900 Synthetic Way, Los Angeles, California 90001",
                    "caller_confirmed": True,
                },
            )
        )
        self.assertIn(
            "UPDATED_SYNTHETIC_MEMBER",
            updated.get_json()["results"][0]["result"],
        )
        context = self.post(
            tool_request(
                "call-update", "tool-context-updated", "get_verified_member_context", {}
            )
        )
        self.assertIn(
            "900 Synthetic Way",
            context.get_json()["results"][0]["result"],
        )
        reloaded = DemoDirectory.from_file(self.path, PEPPER)
        self.assertEqual(
            reloaded.member("james-thornton").safe_context["mailing_address"],
            "900 Synthetic Way, Los Angeles, California 90001",
        )

    def test_completed_renewal_stops_the_old_checklist(self):
        self.post(assistant_request("call-complete", "+14255550123"))
        response = self.post(
            tool_request(
                "call-complete",
                "tool-status",
                "record_renewal_status",
                {"status": "completed"},
            )
        )
        result = response.get_json()["results"][0]["result"]
        self.assertIn("never resume packet or renewal-help questions", result)
        self.assertEqual(
            self.workflow.session("call-complete").renewal_status, "completed"
        )

    def test_callback_tool_blocks_busy_and_respects_no(self):
        self.post(assistant_request("call-consent", "+14255550123"))
        busy = self.post(
            tool_request(
                "call-consent",
                "tool-busy",
                "record_contact_preference",
                {
                    "preference": "callback_requested",
                    "caller_words": "I'm busy right now",
                },
            )
        )
        self.assertIn("BLOCKED", busy.get_json()["results"][0]["result"])

        declined = self.post(
            tool_request(
                "call-consent",
                "tool-no",
                "record_contact_preference",
                {"preference": "no_callback", "caller_words": "Do not call me"},
            )
        )
        self.assertIn("callback declined", declined.get_json()["results"][0]["result"])

        retried = self.post(
            tool_request(
                "call-consent",
                "tool-retry",
                "record_contact_preference",
                {
                    "preference": "callback_requested",
                    "caller_words": "Please call me tomorrow",
                },
            )
        )
        self.assertIn("BLOCKED", retried.get_json()["results"][0]["result"])
        self.assertIn(
            "permanently closed", retried.get_json()["results"][0]["result"]
        )

    def test_callback_decline_at_offer_returns_completion_and_never_reoffers(self):
        call_id = "call-callback-no"
        self.post(assistant_request(call_id, "+14255550123"))
        for index, (field, value) in enumerate(
            (
                ("medi_cal_status", "active"),
                ("pcp_preference", "keep_clinic"),
                ("packet_received", "yes"),
                ("help_preference", "phone"),
                ("contact_information_changed", "no"),
            )
        ):
            self.post(
                tool_request(
                    call_id,
                    f"tool-fact-{index}",
                    "record_workflow_fact",
                    {"field": field, "value": value},
                )
            )

        declined = self.post(
            tool_request(
                call_id,
                "tool-callback-no",
                "record_contact_preference",
                {"preference": "no_callback", "caller_words": "No thanks"},
            )
        ).get_json()["results"][0]["result"]
        self.assertIn("permanently closed", declined)
        self.assertIn('"step":"complete"', declined)
        self.assertIn("hang up now", declined)
        self.assertNotIn('"step":"callback_offer"', declined)

    def test_explicit_callback_flow_collects_structured_details(self):
        self.post(assistant_request("call-callback", "+14255550123"))
        consent = self.post(
            tool_request(
                "call-callback",
                "tool-consent",
                "record_contact_preference",
                {
                    "preference": "callback_requested",
                    "caller_words": "Please call me back",
                    "channel": "phone",
                },
            )
        )
        self.assertIn(
            "preferred day and time window",
            consent.get_json()["results"][0]["result"],
        )

        completed = self.post(
            tool_request(
                "call-callback",
                "tool-details",
                "record_contact_preference",
                {
                    "preference": "callback_requested",
                    "caller_words": "Please call me tomorrow afternoon",
                    "requested_time": "tomorrow from 2 to 4 PM",
                    "channel": "phone",
                    "purpose": "renewal packet help",
                    "timezone": "Pacific",
                    "language": "English",
                },
            )
        )
        result = completed.get_json()["results"][0]["result"]
        self.assertIn("structured callback request", result)
        self.assertIn("does not actually schedule outreach", result)
        self.assertIn("Follow NEXT_STEP exactly", result)
        self.assertEqual(
            self.workflow.session("call-callback").callback_request["purpose"],
            "renewal packet help",
        )

    def test_end_report_expires_verification_session(self):
        self.post(assistant_request("call-6", "+14255550123"))
        ended = self.post(
            {"message": {"type": "end-of-call-report", "call": {"id": "call-6"}}}
        )
        self.assertEqual(ended.get_json(), {"ok": True})
        response = self.post(
            tool_request(
                "call-6", "tool-1", "confirm_synthetic_candidate", {}
            )
        )
        self.assertIn("Unknown or expired", response.get_json()["results"][0]["error"])

    def test_unsupported_tool_returns_flat_error_with_http_200(self):
        self.post(assistant_request("call-7", "+14255550123"))
        response = self.post(tool_request("call-7", "tool-x", "reveal_member", {}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["results"],
            [{"toolCallId": "tool-x", "error": "Unsupported tool call"}],
        )

    def test_phone_number_payload_configures_dynamic_inbound_webhook(self):
        payload = phone_number_payload(
            public_base_url="https://demo.example/",
            webhook_secret=WEBHOOK_SECRET,
            area_code="425",
        )
        self.assertEqual(payload["provider"], "vapi")
        self.assertEqual(payload["numberDesiredAreaCode"], "425")
        self.assertIsNone(payload["assistantId"])
        self.assertEqual(payload["server"]["url"], "https://demo.example/vapi/webhook")
        self.assertEqual(
            payload["server"]["headers"]["Authorization"],
            f"Bearer {WEBHOOK_SECRET}",
        )


if __name__ == "__main__":
    unittest.main()
