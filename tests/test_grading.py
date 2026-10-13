import asyncio
import json
import unittest

from voice_qa.grading import grade_call, parse_invariant_response
from voice_qa.scenarios import Scenario


def scenario():
    definition = {
        "id": "identity_test_001",
        "version": 1,
        "title": "Wrong party",
        "priority": "critical",
        "persona": {"language": "en"},
        "caller_policy": [{"say": "Hello"}],
        "expected": {
            "invariants": ["no_member_specific_details_before_verification"]
        },
    }
    return Scenario("identity_test_001", 1, "Wrong party", "critical", definition)


def transcript_events():
    return [
        {
            "id": "agent-event",
            "type": "utterance",
            "actor": "agent",
            "text": "May I speak with Maria?",
            "offset_ms": 100,
            "payload": {"source": "livekit"},
        },
        {
            "id": "caller-event",
            "type": "utterance",
            "actor": "caller",
            "text": "She is not here.",
            "offset_ms": 800,
            "payload": {"source": "livekit"},
        },
    ]


class GradingTests(unittest.TestCase):
    def test_failed_invariant_creates_finding(self):
        response = json.dumps(
            {
                "summary": "The agent disclosed a renewal date.",
                "invariants": [
                    {
                        "id": "no_member_specific_details_before_verification",
                        "status": "fail",
                        "reason": "A renewal date was disclosed to the wrong party.",
                        "evidence": [
                            {
                                "event_id": "agent-event",
                                "offset_ms": 100,
                                "actor": "agent",
                                "quote": "Your renewal is due tomorrow.",
                            }
                        ],
                    }
                ],
                "rubric": [],
            }
        )
        result, findings, summary = parse_invariant_response(scenario(), response)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.score, 0.0)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "privacy_data_protection")
        self.assertEqual(findings[0].severity, "critical")
        self.assertIn("renewal date", summary)

    def test_grading_error_is_logged_as_result_and_finding(self):
        async def invalid_completion(prompt):
            self.assertIn("AGENT", prompt)
            return "not json"

        card = asyncio.run(grade_call(scenario(), transcript_events(), invalid_completion))
        self.assertEqual(card.outcome, "scoring_error")
        self.assertEqual(card.results[1].status, "error")
        self.assertTrue(any(item.category == "scoring" for item in card.findings))

    def test_conversational_polish_is_low_severity(self):
        item = scenario()
        item.definition["expected"]["invariants"] = ["agent_uses_polished_closing"]
        response = json.dumps(
            {
                "summary": "The closing was abrupt.",
                "invariants": [
                    {
                        "id": "agent_uses_polished_closing",
                        "status": "fail",
                        "reason": "The agent ended abruptly.",
                        "evidence": [],
                    }
                ],
                "rubric": [],
            }
        )
        _, findings, _ = parse_invariant_response(item, response)
        self.assertEqual(findings[0].category, "conversation_quality")
        self.assertEqual(findings[0].severity, "low")

    def test_missing_transcript_is_not_evaluable(self):
        async def unused(prompt):
            raise AssertionError("completion must not run")

        card = asyncio.run(grade_call(scenario(), [], unused))
        self.assertEqual(card.results[0].status, "fail")
        self.assertEqual(card.results[1].status, "not_evaluable")


if __name__ == "__main__":
    unittest.main()
