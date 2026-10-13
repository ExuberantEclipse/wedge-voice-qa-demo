import tempfile
import unittest
from pathlib import Path

from voice_qa.scenarios import (
    Scenario,
    ScenarioValidationError,
    discover_scenarios,
    load_scenario,
    validate_definition,
)


class ScenarioTests(unittest.TestCase):
    def test_portal_language_can_differ_from_caller_language(self):
        definition = {
            "id": "language_mismatch_001",
            "version": 1,
            "title": "Language mismatch",
            "priority": "high",
            "persona": {"language": "en", "demeanor": "neutral"},
            "portal_language": "es",
            "caller_policy": [{"say": "Hello"}],
            "expected": {"invariants": []},
        }
        scenario = Scenario(
            definition["id"],
            definition["version"],
            definition["title"],
            definition["priority"],
            definition,
        )
        self.assertEqual(scenario.primary_language, "en")
        self.assertEqual(scenario.portal_language, "es")

    def test_repository_scenarios_are_valid(self):
        root = Path(__file__).resolve().parents[1] / "testcases"
        scenarios = discover_scenarios(root)
        self.assertGreaterEqual(len(scenarios), 5)

    def test_rejects_missing_policy(self):
        definition = {
            "id": "valid_id_001",
            "version": 1,
            "title": "Example",
            "priority": "high",
            "persona": {"language": "en"},
            "expected": {"invariants": []},
        }
        errors = validate_definition(definition)
        self.assertTrue(any("caller_policy" in error for error in errors))

    def test_rejects_duplicate_id_and_version(self):
        text = """{
          "id": "duplicate_001",
          "version": 1,
          "title": "Duplicate",
          "priority": "low",
          "persona": {"language": "en"},
          "caller_policy": [{"say": "hello"}],
          "expected": {"invariants": []}
        }"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.json").write_text(text, encoding="utf-8")
            (root / "two.json").write_text(text, encoding="utf-8")
            with self.assertRaises(ScenarioValidationError):
                discover_scenarios(root)


if __name__ == "__main__":
    unittest.main()

