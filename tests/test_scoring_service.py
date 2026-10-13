import unittest

from voice_qa.grading import FindingCandidate, GraderResult, Scorecard
from voice_qa.scoring_service import _persist_scorecard


class FakeStore:
    def __init__(self):
        self.calls = []

    def upsert_grader_result(self, **values):
        self.calls.append(("upsert_grader_result", values))

    def upsert_finding(self, **values):
        self.calls.append(("upsert_finding", values))
        return {"id": "current-finding-id"}

    def link_finding(self, finding_id, attempt_id, evidence_note, offset_ms):
        self.calls.append(
            (
                "link_finding",
                finding_id,
                attempt_id,
                evidence_note,
                offset_ms,
            )
        )

    def remove_stale_finding_occurrences(self, attempt_id, active_finding_ids):
        self.calls.append(
            ("remove_stale_finding_occurrences", attempt_id, active_finding_ids)
        )

    def set_attempt_outcome(self, attempt_id, outcome):
        self.calls.append(("set_attempt_outcome", attempt_id, outcome))


class ScorecardPersistenceTests(unittest.TestCase):
    def test_pass_removes_all_previous_finding_occurrences(self):
        store = FakeStore()
        scorecard = Scorecard(
            results=[GraderResult("scenario.invariants", "1", "pass", 100.0)],
            findings=[],
            summary="passed",
        )

        _persist_scorecard(store, "attempt-id", scorecard)

        self.assertIn(
            ("remove_stale_finding_occurrences", "attempt-id", []), store.calls
        )
        self.assertEqual(store.calls[-1], ("set_attempt_outcome", "attempt-id", "passed"))

    def test_failed_score_keeps_only_current_findings(self):
        store = FakeStore()
        scorecard = Scorecard(
            results=[GraderResult("scenario.invariants", "1", "fail", 0.0)],
            findings=[
                FindingCandidate(
                    signature="signature",
                    title="Bad behavior",
                    category="conversation",
                    severity="high",
                    actual_behavior="wrong response",
                    expected_behavior="correct response",
                    reproduction_steps="run the scenario",
                    evidence_note="quoted evidence",
                    offset_ms=500,
                )
            ],
            summary="failed",
        )

        _persist_scorecard(store, "attempt-id", scorecard)

        self.assertIn(
            (
                "remove_stale_finding_occurrences",
                "attempt-id",
                ["current-finding-id"],
            ),
            store.calls,
        )


if __name__ == "__main__":
    unittest.main()
