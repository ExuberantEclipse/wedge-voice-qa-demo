"""Load and validate versioned simulated-caller scenarios."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
PRIORITIES = {"critical", "high", "medium", "low"}


class ScenarioValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    id: str
    version: int
    title: str
    priority: str
    definition: Mapping[str, Any]
    source_path: Optional[Path] = None

    @property
    def persona(self) -> Mapping[str, Any]:
        return self.definition["persona"]

    @property
    def primary_language(self) -> str:
        language = str(self.persona.get("language", "en")).lower()
        return language if language in {"en", "es"} else "en"

    @property
    def portal_language(self) -> str:
        """Language the Wedge agent is configured to expect for this call."""

        language = str(self.definition.get("portal_language", self.primary_language)).lower()
        return language if language in {"en", "es"} else self.primary_language

    @property
    def caller_policy(self) -> List[Mapping[str, Any]]:
        return list(self.definition["caller_policy"])

    @property
    def expected_invariants(self) -> List[str]:
        expected = self.definition.get("expected", {})
        return list(expected.get("invariants", []))

    def as_record(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "priority": self.priority,
            "definition": dict(self.definition),
        }


def _parse_file(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as error:  # pragma: no cover - exercised before install only
            raise RuntimeError("Install PyYAML to load YAML scenarios") from error
        value = yaml.safe_load(text)

    if not isinstance(value, Mapping):
        raise ScenarioValidationError(f"{path}: top-level value must be an object")
    return value


def validate_definition(
    definition: Mapping[str, Any], source: str = "scenario"
) -> List[str]:
    errors: List[str] = []
    scenario_id = definition.get("id")
    if not isinstance(scenario_id, str) or not ID_PATTERN.fullmatch(scenario_id):
        errors.append(f"{source}: id must match {ID_PATTERN.pattern}")

    version = definition.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(f"{source}: version must be a positive integer")

    if not isinstance(definition.get("title"), str) or not definition["title"].strip():
        errors.append(f"{source}: title is required")

    if definition.get("priority") not in PRIORITIES:
        errors.append(f"{source}: priority must be one of {sorted(PRIORITIES)}")

    persona = definition.get("persona")
    if not isinstance(persona, Mapping):
        errors.append(f"{source}: persona must be an object")
    elif persona.get("language", "en") not in {"en", "es", "mixed", "unsupported"}:
        errors.append(f"{source}: persona.language is not supported")

    portal_language = definition.get("portal_language", "en")
    if portal_language not in {"en", "es"}:
        errors.append(f"{source}: portal_language must be en or es")

    policy = definition.get("caller_policy")
    if not isinstance(policy, list) or not policy:
        errors.append(f"{source}: caller_policy must be a non-empty list")
    elif not all(isinstance(step, Mapping) and step for step in policy):
        errors.append(f"{source}: every caller_policy entry must be a non-empty object")

    expected = definition.get("expected")
    if not isinstance(expected, Mapping):
        errors.append(f"{source}: expected must be an object")
    elif not isinstance(expected.get("invariants"), list):
        errors.append(f"{source}: expected.invariants must be a list")

    max_duration = definition.get("max_duration_seconds", 240)
    if not isinstance(max_duration, int) or not 30 <= max_duration <= 900:
        errors.append(f"{source}: max_duration_seconds must be between 30 and 900")

    return errors


def load_scenario(path: Path) -> Scenario:
    definition = _parse_file(path)
    errors = validate_definition(definition, str(path))
    if errors:
        raise ScenarioValidationError("\n".join(errors))
    return Scenario(
        id=str(definition["id"]),
        version=int(definition["version"]),
        title=str(definition["title"]),
        priority=str(definition["priority"]),
        definition=definition,
        source_path=path,
    )


def discover_scenarios(root: Path) -> List[Scenario]:
    paths: Iterable[Path] = sorted(
        list(root.rglob("*.yaml"))
        + list(root.rglob("*.yml"))
        + [path for path in root.rglob("*.json") if path.name != "schema.json"]
    )
    scenarios = [load_scenario(path) for path in paths]
    keys = [(scenario.id, scenario.version) for scenario in scenarios]
    if len(keys) != len(set(keys)):
        raise ScenarioValidationError("Duplicate scenario id/version found")
    return scenarios


def find_scenario(root: Path, scenario_id: str) -> Scenario:
    matches = [item for item in discover_scenarios(root) if item.id == scenario_id]
    if not matches:
        raise ScenarioValidationError(f"Unknown scenario: {scenario_id}")
    return max(matches, key=lambda item: item.version)
