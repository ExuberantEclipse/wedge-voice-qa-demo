import unittest

from voice_qa.review import build_error_log, render_error_log


class ReviewTests(unittest.TestCase):
    def report(self):
        return build_error_log(
            attempts=[
                {
                    "id": "attempt-1",
                    "assignment_id": "assignment-1",
                    "test_run_id": "run-1",
                    "test_case_id": "callback_case",
                    "test_case_version": 1,
                    "status": "completed",
                    "outcome": "failed_checks",
                    "started_at": "2026-10-10T10:00:00Z",
                }
            ],
            assignments=[
                {
                    "id": "assignment-1",
                    "scenario_snapshot": {
                        "id": "callback_case",
                        "title": "Caller declines callback",
                        "persona": {"language": "en", "demeanor": "rushed"},
                        "caller_policy": [
                            {"say": "Do not call me back."},
                        ],
                    },
                }
            ],
            findings=[
                {
                    "id": "finding-1",
                    "signature": "callback:test",
                    "title": "Unsafe <callback>",
                    "category": "consent_contact_preferences",
                    "severity": "critical",
                    "status": "candidate",
                    "actual_behavior": "Agent offered another callback.",
                    "expected_behavior": "Respect the refusal.",
                    "reproduction_steps": "Run the callback case.",
                    "suspected_cause": None,
                }
            ],
            occurrences=[
                {
                    "id": "occurrence-1",
                    "finding_id": "finding-1",
                    "call_attempt_id": "attempt-1",
                    "offset_ms": 2300,
                    "evidence_note": "The caller said no.",
                }
            ],
            events=[
                {
                    "id": "portal-result",
                    "call_attempt_id": "attempt-1",
                    "sequence": 3,
                    "type": "portal_call_ingested",
                    "actor": "system",
                    "text": "",
                    "offset_ms": None,
                    "payload": {"recording_url": "https://example.test/recording.mp3"},
                },
            ],
            artifacts=[],
        )

    def test_builds_reproduction_frequency_and_recording_reference(self):
        report = self.report()
        self.assertEqual(report["summary"]["findings"], 1)
        self.assertEqual(report["summary"]["occurrences"], 1)
        issue = report["issues"][0]
        self.assertIn("Observed: 1 of 1 stored attempt", issue["how_to_reproduce"])
        self.assertIn("Do not call me back.", issue["how_to_reproduce"])
        self.assertEqual(
            issue["recordings"][0]["url"], "https://example.test/recording.mp3"
        )
        self.assertEqual(issue["recordings"][0]["timestamp"], "00:02")

    def test_html_is_filterable_and_escapes_database_content(self):
        rendered = render_error_log(self.report())
        self.assertIn('id="search"', rendered)
        self.assertIn('data-severity="critical"', rendered)
        self.assertIn("QA-001", rendered)
        self.assertIn("Unsafe &lt;callback&gt;", rendered)
        self.assertNotIn("Unsafe <callback>", rendered)
        self.assertIn("Open recording", rendered)
        for label in (
            "ID / title",
            "Category",
            "Severity",
            "Recording + timestamp",
            "What happened",
            "What should have happened",
            "How to reproduce",
        ):
            self.assertIn(label, rendered)
        self.assertNotIn("Full transcript", rendered)
        self.assertNotIn("Grader verdict", rendered)


if __name__ == "__main__":
    unittest.main()
