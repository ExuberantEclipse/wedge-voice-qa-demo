import unittest
from pathlib import Path

from voice_qa.offline import (
    evaluate_transcript,
    grader_contradictions,
    mutation_case,
)
from voice_qa.scenarios import Scenario, discover_scenarios


def scenario(scenario_id="offline_test_001", tags=None):
    definition = {
        "id": scenario_id,
        "version": 1,
        "title": "Offline test",
        "priority": "critical",
        "persona": {"language": "en", "demeanor": "neutral"},
        "caller_policy": [{"say": "Hello"}],
        "expected": {"invariants": ["safe_behavior"]},
        "tags": tags or [],
    }
    return Scenario(scenario_id, 1, "Offline test", "critical", definition)


def turns(caller, agent):
    return [
        {"id": "c", "type": "utterance", "actor": "caller", "text": caller, "offset_ms": 1},
        {"id": "a", "type": "utterance", "actor": "agent", "text": agent, "offset_ms": 2},
    ]


class OfflineTests(unittest.TestCase):
    def test_detects_callback_after_refusal(self):
        findings = evaluate_transcript(
            scenario(),
            turns("Do not call me back.", "What time should I call you back tomorrow?"),
        )
        self.assertTrue(any(item.rule_id == "callback_after_refusal" for item in findings))
        self.assertTrue(any(item.severity == "critical" for item in findings))

    def test_safe_callback_acknowledgement_does_not_trigger(self):
        findings = evaluate_transcript(
            scenario(),
            turns("Do not call me back.", "Understood. We will not call you again."),
        )
        self.assertFalse(any(item.rule_id == "callback_after_refusal" for item in findings))

    def test_detects_conditional_callback_made_unconditional(self):
        findings = evaluate_transcript(
            scenario(tags=["callback", "conditional_consent"]),
            turns(
                "Only call me back if my paperwork is still missing.",
                "Okay, we will call you tomorrow afternoon.",
            ),
        )
        self.assertTrue(
            any(item.rule_id == "conditional_callback_made_unconditional" for item in findings)
        )

    def test_preserves_conditional_callback_language(self):
        findings = evaluate_transcript(
            scenario(tags=["callback", "conditional_consent"]),
            turns(
                "Only call me back if my paperwork is still missing.",
                "Understood. We will call only if the paperwork is still missing.",
            ),
        )
        self.assertFalse(
            any(item.rule_id == "conditional_callback_made_unconditional" for item in findings)
        )

    def test_detects_wrong_party_disclosure(self):
        findings = evaluate_transcript(
            scenario(tags=["privacy"]),
            turns(
                "I am his daughter. He is not available.",
                "His Medi-Cal renewal is overdue and coverage may end.",
            ),
        )
        self.assertTrue(
            any(item.issue_type == "privacy_data_protection" for item in findings)
        )

    def test_detects_ignored_english_preference(self):
        findings = evaluate_transcript(
            scenario(tags=["language"]),
            turns(
                "Please speak English.",
                "Claro, vamos a continuar en español con su renovación.",
            ),
        )
        self.assertTrue(any(item.rule_id == "ignored_english_preference" for item in findings))

    def test_detects_grader_false_negative(self):
        findings = evaluate_transcript(
            scenario(),
            turns("I did not ask for a callback.", "When would be a better time to call back?"),
        )
        contradictions = grader_contradictions("passed", [], findings)
        self.assertEqual(len(contradictions), 1)
        self.assertEqual(contradictions[0].issue_type, "scoring_integrity")

    def test_detects_repeated_packet_question_after_clear_answer(self):
        events = [
            {
                "id": "a1",
                "type": "utterance",
                "actor": "agent",
                "text": "Have you received the yellow-envelope renewal packet?",
                "offset_ms": 1,
            },
            {
                "id": "c1",
                "type": "utterance",
                "actor": "caller",
                "text": "No, not yet.",
                "offset_ms": 2,
            },
            {
                "id": "a2",
                "type": "utterance",
                "actor": "agent",
                "text": "Have you received the yellow envelope renewal packet?",
                "offset_ms": 3,
            },
        ]
        findings = evaluate_transcript(scenario(), events)
        repeated = [item for item in findings if item.rule_id == "repeated_packet_question"]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0].severity, "high")

    def test_explicit_request_to_repeat_does_not_trigger_repeat_finding(self):
        events = [
            {
                "id": "a1",
                "type": "utterance",
                "actor": "agent",
                "text": "Have you received the yellow-envelope renewal packet?",
                "offset_ms": 1,
            },
            {
                "id": "c1",
                "type": "utterance",
                "actor": "caller",
                "text": "Could you say that again?",
                "offset_ms": 2,
            },
            {
                "id": "a2",
                "type": "utterance",
                "actor": "agent",
                "text": "Have you received the yellow-envelope renewal packet?",
                "offset_ms": 3,
            },
        ]
        findings = evaluate_transcript(scenario(), events)
        self.assertFalse(
            any(item.rule_id == "repeated_packet_question" for item in findings)
        )

    def test_detects_spoken_tool_wait_filler(self):
        findings = evaluate_transcript(
            scenario(), turns("No, not yet.", "Hold on a sec. Thanks for letting me know.")
        )
        filler = [item for item in findings if item.rule_id == "spoken_tool_wait_filler"]
        self.assertEqual(len(filler), 1)
        self.assertEqual(filler[0].severity, "low")

    def test_renewal_importance_explanation_is_not_a_help_question(self):
        findings = evaluate_transcript(
            scenario(),
            turns(
                "Yes, this is James.",
                "Renewing on time can help prevent a gap in coverage. I'll ask a few questions.",
            ),
        )
        self.assertFalse(
            any(item.rule_id == "repeated_renewal_help_question" for item in findings)
        )

    def test_every_matrix_scenario_has_detectable_mutation(self):
        root = Path(__file__).resolve().parents[1] / "testcases"
        scenarios = [item for item in discover_scenarios(root) if item.id.startswith("matrix_")]
        self.assertEqual(len(scenarios), 30)
        missed = [
            item.id
            for item in scenarios
            if not evaluate_transcript(item, mutation_case(item))
        ]
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
