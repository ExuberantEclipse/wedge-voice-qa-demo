import tempfile
import unittest
from pathlib import Path

from twilio.request_validator import RequestValidator

from voice_qa.caller_identity import DemoDirectory, IdentityController, seed_member
from voice_qa.twilio_demo import create_app


PEPPER = "test-pepper-that-is-at-least-thirty-two-characters"
AUTH_TOKEN = "test-auth-token"
PUBLIC_URL = "https://wedge-demo.example"


class TwilioDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "members.json"
        seed_member(
            path=path,
            pepper=PEPPER,
            member_id="james-thornton",
            display_name="James Thornton",
            phone="+14255550123",
            pin="2468",
            safe_context={"renewal_summary": "Your synthetic renewal was received."},
        )
        self.controller = IdentityController(DemoDirectory.from_file(path, PEPPER))
        app = create_app(
            controller=self.controller,
            auth_token=AUTH_TOKEN,
            public_base_url=PUBLIC_URL,
            validate_signatures=False,
        )
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_known_caller_is_asked_for_pin_without_member_disclosure(self):
        response = self.client.post(
            "/twilio/voice",
            data={"CallSid": "CA-1", "From": "+14255550123"},
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("four digit demo code", body)
        self.assertNotIn("James", body)
        self.assertNotIn("renewal", body)

    def test_context_is_spoken_only_after_correct_pin(self):
        self.client.post(
            "/twilio/voice",
            data={"CallSid": "CA-1", "From": "+14255550123"},
        )
        response = self.client.post(
            "/twilio/verify", data={"CallSid": "CA-1", "Digits": "2468"}
        )
        body = response.get_data(as_text=True)
        self.assertIn("Hello James Thornton", body)
        self.assertIn("synthetic renewal was received", body)

    def test_unknown_caller_gets_generic_name_prompt(self):
        response = self.client.post(
            "/twilio/voice",
            data={"CallSid": "CA-2", "From": "+14255559999"},
        )
        body = response.get_data(as_text=True)
        self.assertIn("say the full name", body)
        self.assertNotIn("James", body)

    def test_wrong_pin_retries_then_locks(self):
        self.client.post(
            "/twilio/voice",
            data={"CallSid": "CA-3", "From": "+14255550123"},
        )
        for _ in range(2):
            response = self.client.post(
                "/twilio/verify", data={"CallSid": "CA-3", "Digits": "0000"}
            )
            self.assertIn("did not match", response.get_data(as_text=True))
        response = self.client.post(
            "/twilio/verify", data={"CallSid": "CA-3", "Digits": "0000"}
        )
        self.assertIn("locked after too many attempts", response.get_data(as_text=True))

    def test_status_callback_expires_call_session(self):
        self.client.post(
            "/twilio/voice",
            data={"CallSid": "CA-4", "From": "+14255550123"},
        )
        response = self.client.post(
            "/twilio/status", data={"CallSid": "CA-4", "CallStatus": "completed"}
        )
        self.assertEqual(response.status_code, 204)
        response = self.client.post(
            "/twilio/verify", data={"CallSid": "CA-4", "Digits": "2468"}
        )
        self.assertIn("session is unavailable", response.get_data(as_text=True))

    def test_signature_validation_accepts_valid_and_rejects_invalid_requests(self):
        app = create_app(
            controller=self.controller,
            auth_token=AUTH_TOKEN,
            public_base_url=PUBLIC_URL,
            validate_signatures=True,
        )
        app.testing = True
        client = app.test_client()
        params = {"CallSid": "CA-SIGNED", "From": "+14255550123"}
        signature = RequestValidator(AUTH_TOKEN).compute_signature(
            PUBLIC_URL + "/twilio/voice", params
        )
        accepted = client.post(
            "/twilio/voice",
            data=params,
            headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(accepted.status_code, 200)

        rejected = client.post(
            "/twilio/voice",
            data={"CallSid": "CA-FORGED", "From": "+14255550123"},
            headers={"X-Twilio-Signature": "invalid"},
        )
        self.assertEqual(rejected.status_code, 403)


if __name__ == "__main__":
    unittest.main()
