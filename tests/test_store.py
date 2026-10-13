import json
import unittest
from unittest.mock import patch

from voice_qa.scenarios import Scenario
from voice_qa.store import Assignment, SupabaseStore


class FakeResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class StoreTests(unittest.TestCase):
    @patch("voice_qa.store.urlopen")
    def test_upsert_test_cases_sends_one_bulk_request(self, urlopen):
        scenarios = [
            Scenario(
                id="case_one",
                version=1,
                title="Case one",
                priority="high",
                definition={"id": "case_one", "version": 1},
            ),
            Scenario(
                id="case_two",
                version=2,
                title="Case two",
                priority="low",
                definition={"id": "case_two", "version": 2},
            ),
        ]
        urlopen.return_value = FakeResponse(
            [scenario.as_record() for scenario in scenarios]
        )

        store = SupabaseStore("https://project.supabase.co", "secret")
        rows = store.upsert_test_cases(scenarios)

        self.assertEqual(len(rows), 2)
        request = urlopen.call_args.args[0]
        self.assertIn("on_conflict=id,version", request.full_url)
        self.assertEqual(request.method, "POST")
        self.assertEqual(len(json.loads(request.data)), 2)

    @patch("voice_qa.store.urlopen")
    def test_claim_next_maps_assignment(self, urlopen):
        urlopen.return_value = FakeResponse(
            [
                {
                    "id": "assignment-id",
                    "test_run_id": "run-id",
                    "test_case_id": "case-id",
                    "test_case_version": 2,
                    "called_number": "+14155550123",
                    "scenario_snapshot": {"id": "case-id"},
                }
            ]
        )
        store = SupabaseStore("https://project.supabase.co", "secret")
        assignment = store.claim_next("+14155550123", "room", "sip-call")
        self.assertIsInstance(assignment, Assignment)
        self.assertEqual(assignment.test_case_version, 2)

        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/rest/v1/rpc/claim_next_call_assignment"))
        self.assertNotIn("secret", request.full_url)

    @patch("voice_qa.store.urlopen")
    def test_claim_next_returns_none_for_empty_queue(self, urlopen):
        urlopen.return_value = FakeResponse([])
        store = SupabaseStore("https://project.supabase.co", "secret")
        self.assertIsNone(store.claim_next("+14155550123", "room", None))

    @patch("voice_qa.store.urlopen")
    def test_start_manual_attempt_claims_without_livekit(self, urlopen):
        urlopen.side_effect = [
            FakeResponse(None),
            FakeResponse(None),
            FakeResponse([{"id": "attempt-id"}]),
        ]
        assignment = {
            "id": "assignment-id",
            "test_run_id": "run-id",
            "test_case_id": "manual-case",
            "test_case_version": 1,
        }

        store = SupabaseStore("https://project.supabase.co", "secret")
        attempt_id = store.start_manual_attempt(assignment, "portal-call-id")

        self.assertEqual(attempt_id, "attempt-id")
        assignment_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(assignment_request.method, "PATCH")
        self.assertIn("call_assignments", assignment_request.full_url)
        attempt_request = urlopen.call_args_list[2].args[0]
        self.assertEqual(attempt_request.method, "POST")
        payload = json.loads(attempt_request.data)
        self.assertEqual(payload["external_call_id"], "portal-call-id")
        self.assertEqual(payload["status"], "in_progress")
        self.assertIsNone(payload.get("livekit_room"))

    @patch("voice_qa.store.urlopen")
    def test_append_events_uses_one_bulk_request(self, urlopen):
        urlopen.return_value = FakeResponse(
            [
                {
                    "id": "event-id",
                    "call_attempt_id": "attempt-id",
                    "type": "utterance",
                }
            ]
        )
        store = SupabaseStore("https://project.supabase.co", "secret")
        rows = store.append_events(
            "attempt-id",
            [
                {
                    "type": "utterance",
                    "actor": "agent",
                    "text": "hello",
                    "offset_ms": 100,
                    "payload": {"source": "livekit"},
                }
            ],
        )
        self.assertEqual(rows[0]["id"], "event-id")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload[0]["call_attempt_id"], "attempt-id")
        self.assertEqual(payload[0]["offset_ms"], 100)

    @patch("voice_qa.store.urlopen")
    def test_link_finding_refreshes_existing_evidence(self, urlopen):
        urlopen.side_effect = [
            FakeResponse([{"id": "occurrence-id"}]),
            FakeResponse(
                [
                    {
                        "id": "occurrence-id",
                        "evidence_note": "latest evidence",
                        "offset_ms": 250,
                    }
                ]
            ),
        ]

        store = SupabaseStore("https://project.supabase.co", "secret")
        occurrence = store.link_finding(
            "finding-id", "attempt-id", "latest evidence", 250
        )

        self.assertEqual(occurrence["evidence_note"], "latest evidence")
        update_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(update_request.method, "PATCH")
        self.assertIn("id=eq.occurrence-id", update_request.full_url)
        self.assertEqual(
            json.loads(update_request.data),
            {"offset_ms": 250, "evidence_note": "latest evidence"},
        )

    @patch("voice_qa.store.urlopen")
    def test_remove_stale_findings_keeps_current_finding_ids(self, urlopen):
        urlopen.return_value = FakeResponse(None)

        store = SupabaseStore("https://project.supabase.co", "secret")
        store.remove_stale_finding_occurrences(
            "attempt-id", ["current-finding-id"]
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertIn("call_attempt_id=eq.attempt-id", request.full_url)
        self.assertIn("finding_id=not.in.%28current-finding-id%29", request.full_url)

    @patch("voice_qa.store.urlopen")
    def test_remove_stale_findings_removes_all_when_score_passes(self, urlopen):
        urlopen.return_value = FakeResponse(None)

        store = SupabaseStore("https://project.supabase.co", "secret")
        store.remove_stale_finding_occurrences("attempt-id", [])

        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertIn("call_attempt_id=eq.attempt-id", request.full_url)
        self.assertNotIn("finding_id", request.full_url)

    @patch("voice_qa.store.urlopen")
    def test_finding_occurrences_for_attempts_uses_bulk_filter(self, urlopen):
        urlopen.return_value = FakeResponse([])

        store = SupabaseStore("https://project.supabase.co", "secret")
        rows = store.finding_occurrences_for_attempts(["attempt-1", "attempt-2"])

        self.assertEqual(rows, [])
        request = urlopen.call_args.args[0]
        self.assertIn("call_attempt_id=in.%28attempt-1%2Cattempt-2%29", request.full_url)
        self.assertNotIn("secret", request.full_url)


if __name__ == "__main__":
    unittest.main()
