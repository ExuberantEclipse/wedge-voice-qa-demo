import json
import tempfile
import unittest
from pathlib import Path

from voice_qa.caller_identity import (
    DemoDirectory,
    IdentityController,
    IdentityError,
    seed_member,
)


PEPPER = "test-pepper-that-is-at-least-thirty-two-characters"


def add_member(
    path: Path,
    *,
    member_id: str = "james-thornton",
    name: str = "James Thornton",
    phone: str = "+14255550123",
    pin: str = "2468",
) -> None:
    seed_member(
        path=path,
        pepper=PEPPER,
        member_id=member_id,
        display_name=name,
        phone=phone,
        pin=pin,
        safe_context={"renewal_summary": "Your synthetic renewal was received."},
    )


class DemoDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "members.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_stores_neither_raw_phone_nor_pin(self):
        add_member(self.path)
        serialized = self.path.read_text(encoding="utf-8")
        self.assertNotIn("+14255550123", serialized)
        self.assertNotIn('"2468"', serialized)
        self.assertEqual(json.loads(serialized)["version"], 1)

    def test_unique_shared_and_unknown_number_candidates(self):
        add_member(self.path)
        directory = DemoDirectory.from_file(self.path, PEPPER)
        self.assertEqual(directory.candidates("(425) 555-0123"), ("james-thornton",))
        self.assertEqual(directory.candidates("+14255559999"), ())

        add_member(
            self.path,
            member_id="marisol-thornton",
            name="Marisol Thornton",
            phone="+14255550123",
            pin="1357",
        )
        shared = DemoDirectory.from_file(self.path, PEPPER)
        self.assertEqual(
            shared.candidates("+14255550123"),
            ("james-thornton", "marisol-thornton"),
        )


class IdentityControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "members.json"
        add_member(self.path)
        self.controller = IdentityController(DemoDirectory.from_file(self.path, PEPPER))

    def tearDown(self):
        self.temp.cleanup()

    def test_caller_number_is_only_a_candidate_hint(self):
        session = self.controller.start("CA-1", "+14255550123")
        self.assertEqual(session.state, "candidate_unique")
        self.assertIsNone(session.capability_token)
        with self.assertRaises(PermissionError):
            self.controller.member_context("CA-1", "forged")

    def test_correct_pin_issues_call_bound_capability(self):
        self.controller.start("CA-1", "+14255550123")
        session = self.controller.verify_pin("CA-1", "2468")
        self.assertEqual(session.state, "verified_member")
        self.assertIsNotNone(session.capability_token)
        context = self.controller.member_context("CA-1", session.capability_token or "")
        self.assertEqual(context["display_name"], "James Thornton")

        self.controller.start("CA-2", "+14255550123")
        with self.assertRaises(PermissionError):
            self.controller.member_context("CA-2", session.capability_token or "")

    def test_synthetic_candidate_confirmation_unlocks_demo_without_pin(self):
        session = self.controller.start("CA-demo", "+14255550123")
        self.assertEqual(session.state, "candidate_unique")
        confirmed = self.controller.confirm_synthetic_candidate("CA-demo")
        self.assertEqual(confirmed.state, "verified_member")
        self.assertIsNotNone(confirmed.capability_token)
        context = self.controller.member_context(
            "CA-demo", confirmed.capability_token or ""
        )
        self.assertEqual(context["display_name"], "James Thornton")

    def test_verified_confirmed_member_update_persists(self):
        session = self.controller.start("CA-1", "+14255550123")
        session = self.controller.verify_pin("CA-1", "2468")
        updated = self.controller.update_member_context(
            "CA-1",
            session.capability_token or "",
            "mailing_address",
            "900 Synthetic Way, Los Angeles, California 90001",
            caller_confirmed=True,
        )
        self.assertEqual(
            updated["mailing_address"],
            "900 Synthetic Way, Los Angeles, California 90001",
        )
        reloaded = DemoDirectory.from_file(self.path, PEPPER)
        self.assertEqual(
            reloaded.member("james-thornton").safe_context["mailing_address"],
            "900 Synthetic Way, Los Angeles, California 90001",
        )

    def test_member_update_requires_verification_confirmation_and_allowed_field(self):
        session = self.controller.start("CA-1", "+14255550123")
        with self.assertRaisesRegex(PermissionError, "verified identity"):
            self.controller.update_member_context(
                "CA-1",
                "forged",
                "mailing_address",
                "900 Synthetic Way",
                caller_confirmed=True,
            )

        session = self.controller.verify_pin("CA-1", "2468")
        with self.assertRaisesRegex(IdentityError, "explicit caller confirmation"):
            self.controller.update_member_context(
                "CA-1",
                session.capability_token or "",
                "mailing_address",
                "900 Synthetic Way",
                caller_confirmed=False,
            )
        with self.assertRaisesRegex(IdentityError, "Unsupported synthetic member field"):
            self.controller.update_member_context(
                "CA-1",
                session.capability_token or "",
                "renewal_status",
                "approved",
                caller_confirmed=True,
            )

    def test_three_mismatches_lock_verification(self):
        self.controller.start("CA-1", "+14255550123")
        for _ in range(2):
            session = self.controller.verify_pin("CA-1", "0000")
            self.assertEqual(session.state, "verification_pending")
        session = self.controller.verify_pin("CA-1", "0000")
        self.assertEqual(session.state, "locked")
        self.assertIsNone(session.capability_token)

    def test_ambiguous_number_requires_exact_name_then_pin(self):
        add_member(
            self.path,
            member_id="marisol-thornton",
            name="Marisol Thornton",
            phone="+14255550123",
            pin="1357",
        )
        controller = IdentityController(DemoDirectory.from_file(self.path, PEPPER))
        session = controller.start("CA-1", "+14255550123")
        self.assertEqual(session.state, "candidate_ambiguous")
        self.assertTrue(controller.claim_name("CA-1", "Marisol Thornton"))
        self.assertEqual(controller.verify_pin("CA-1", "1357").state, "verified_member")

    def test_end_revokes_and_removes_session(self):
        self.controller.start("CA-1", "+14255550123")
        self.controller.verify_pin("CA-1", "2468")
        self.controller.end("CA-1")
        with self.assertRaises(IdentityError):
            self.controller.session("CA-1")

    def test_audit_log_has_no_phone_or_pin(self):
        session = self.controller.start("CA-1", "+14255550123")
        self.controller.verify_pin("CA-1", "0000")
        serialized = json.dumps(session.audit_events)
        self.assertNotIn("+14255550123", serialized)
        self.assertNotIn("0000", serialized)

    def test_member_update_audit_omits_updated_value(self):
        session = self.controller.start("CA-1", "+14255550123")
        session = self.controller.verify_pin("CA-1", "2468")
        self.controller.update_member_context(
            "CA-1",
            session.capability_token or "",
            "contact_phone",
            "(213) 555-0199",
            caller_confirmed=True,
        )
        serialized = json.dumps(session.audit_events)
        self.assertNotIn("213", serialized)
        self.assertNotIn("0199", serialized)


if __name__ == "__main__":
    unittest.main()
