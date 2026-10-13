import unittest

from voice_qa.prompts import build_caller_instructions
from voice_qa.scenarios import Scenario


class PromptTests(unittest.TestCase):
    def test_prompt_contains_scenario_and_safety_constraints(self):
        definition = {
            "id": "identity_test_001",
            "version": 1,
            "title": "Wrong party",
            "priority": "critical",
            "persona": {"language": "en", "demeanor": "firm"},
            "caller_policy": [{"say": "Hello?"}],
            "expected": {"invariants": ["identity_gate"]},
        }
        prompt = build_caller_instructions(
            Scenario("identity_test_001", 1, "Wrong party", "critical", definition)
        )
        self.assertIn("identity_test_001 v1", prompt)
        self.assertIn("Do not greet first", prompt)
        self.assertIn("Never reveal that you are a test agent", prompt)


if __name__ == "__main__":
    unittest.main()

