"""CLI for validating and queueing QA scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional, Sequence

from dotenv import load_dotenv

from .config import Settings, is_e164, normalize_phone, project_root
from .offline import (
    evaluate_transcript,
    export_fixture,
    finding_dict,
    grader_contradictions,
    load_fixtures,
    mutation_case,
    report_payload,
    write_fixture,
)
from .portal import PortalError, WedgePortalClient
from .review import build_error_log, write_error_log
from .scenarios import Scenario, ScenarioValidationError, discover_scenarios, find_scenario
from .scoring_service import score_attempt
from .store import StoreError, SupabaseStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-qa")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=project_root() / "testcases",
        help="scenario directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="validate every scenario")
    subparsers.add_parser("list", help="list available scenarios")
    subparsers.add_parser("seed", help="upsert every scenario into Supabase")

    queue = subparsers.add_parser("queue", help="queue one scenario for the next call")
    queue.add_argument("scenario_id")
    queue.add_argument("--called-number", help="dedicated QA DID; defaults to QA_CALLED_NUMBER")
    queue.add_argument("--agent-version", help="label for the Wedge agent under test")
    queue.add_argument("--label", help="run label")

    status = subparsers.add_parser("queue-status", help="show recent assignments")
    status.add_argument("--limit", type=int, default=20)

    batch = subparsers.add_parser(
        "batch-queue", help="queue a sequential batch of scenarios for the QA number"
    )
    batch.add_argument("scenario_ids", nargs="*", help="defaults to every scenario")
    batch.add_argument("--repeat", type=int, default=1)
    batch.add_argument("--called-number", help="defaults to QA_CALLED_NUMBER")
    batch.add_argument("--agent-version", help="label for the Wedge agent under test")
    batch.add_argument("--label", default="voice-qa-batch")

    score = subparsers.add_parser("score", help="rerun scoring for a completed attempt")
    score.add_argument("attempt_id")
    score.add_argument("--model", help="defaults to QA_GRADER_MODEL")

    results = subparsers.add_parser("results", help="show recent call scores and errors")
    results.add_argument("--limit", type=int, default=20)

    error_log = subparsers.add_parser(
        "error-log", help="build one navigable HTML view of grader findings and evidence"
    )
    error_log.add_argument("--limit", type=int, default=100)
    error_log.add_argument(
        "--output", type=Path, default=project_root() / "artifacts" / "error-log.html"
    )

    offline_export = subparsers.add_parser(
        "offline-export", help="export completed transcripts as redacted local fixtures"
    )
    offline_export.add_argument("--prefix", default="matrix_")
    offline_export.add_argument("--limit", type=int, default=100)
    offline_export.add_argument(
        "--output", type=Path, default=project_root() / "fixtures" / "transcripts"
    )

    offline_test = subparsers.add_parser(
        "offline-test", help="run deterministic replay and mutation tests without LiveKit"
    )
    offline_test.add_argument("--prefix", default="matrix_")
    offline_test.add_argument(
        "--fixtures", type=Path, default=project_root() / "fixtures" / "transcripts"
    )
    offline_test.add_argument(
        "--output", type=Path, default=project_root() / "artifacts" / "offline-report.json"
    )

    subparsers.add_parser(
        "portal-personas", help="verify portal credentials and list fictional personas"
    )

    manual_call = subparsers.add_parser(
        "manual-call",
        help="call a human tester and automatically persist portal evidence",
    )
    manual_call.add_argument("scenario_id")
    manual_call.add_argument("--phone", required=True, help="approved phone you will answer")
    manual_call.add_argument("--persona-id", help="defaults to WEDGE_PORTAL_PERSONA_ID")
    manual_call.add_argument("--agent-version", help="label for the Wedge agent under test")
    manual_call.add_argument("--label", default="manual-voice-qa")
    manual_call.add_argument("--timeout", type=int, default=600)

    run_batch = subparsers.add_parser(
        "run-batch", help="queue, originate, ingest, and score portal calls"
    )
    run_batch.add_argument("scenario_ids", nargs="*", help="defaults to every scenario")
    run_batch.add_argument("--repeat", type=int, default=1)
    run_batch.add_argument("--persona-id", help="defaults to WEDGE_PORTAL_PERSONA_ID")
    run_batch.add_argument("--called-number", help="defaults to QA_CALLED_NUMBER")
    run_batch.add_argument("--agent-version", help="label for the Wedge agent under test")
    run_batch.add_argument("--label", default="voice-qa-portal-batch")
    run_batch.add_argument("--timeout", type=int, default=600)
    run_batch.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="parallel repetitions per scenario (default: 1, maximum: 10)",
    )
    return parser


def _store() -> SupabaseStore:
    settings = Settings.from_env(require_supabase=True)
    return SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)


def run(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv(".env.local")
    args = build_parser().parse_args(argv)
    try:
        scenarios = discover_scenarios(args.scenarios)
        if args.command == "validate":
            print(f"Validated {len(scenarios)} scenario(s).")
            return 0
        if args.command == "list":
            for scenario in scenarios:
                print(f"{scenario.id}\tv{scenario.version}\t{scenario.priority}\t{scenario.title}")
            return 0
        if args.command == "seed":
            rows = _store().upsert_test_cases(scenarios)
            print(f"Seeded {len(rows)} scenario(s) into Supabase.")
            return 0
        if args.command == "queue":
            settings = Settings.from_env(require_supabase=True)
            scenario = find_scenario(args.scenarios, args.scenario_id)
            called_number = normalize_phone(args.called_number or settings.called_number or "")
            if not is_e164(called_number):
                raise ValueError(
                    "Set QA_CALLED_NUMBER or pass --called-number in valid E.164 format"
                )
            store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
            store.upsert_test_case(scenario)
            run_row = store.create_run(args.label or scenario.id, args.agent_version)
            assignment = store.queue_assignment(scenario, str(run_row["id"]), called_number)
            print(
                f"Queued {scenario.id} v{scenario.version} for {called_number}: "
                f"assignment {assignment['id']}"
            )
            return 0
        if args.command == "queue-status":
            for row in _store().recent_assignments(max(1, min(args.limit, 100))):
                print(
                    f"{row['created_at']}\t{row['status']}\t{row['test_case_id']}"
                    f"\t{row['called_number']}\t{row['id']}"
                )
            return 0
        if args.command == "batch-queue":
            if args.repeat < 1 or args.repeat > 20:
                raise ValueError("--repeat must be between 1 and 20")
            settings = Settings.from_env(require_supabase=True)
            called_number = normalize_phone(args.called_number or settings.called_number or "")
            if not is_e164(called_number):
                raise ValueError(
                    "Set QA_CALLED_NUMBER or pass --called-number in valid E.164 format"
                )
            selected = scenarios
            if args.scenario_ids:
                selected = [find_scenario(args.scenarios, item) for item in args.scenario_ids]
            store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
            store.upsert_test_cases(selected)
            run_row = store.create_run(args.label, args.agent_version)
            count = 0
            for _ in range(args.repeat):
                for scenario in selected:
                    assignment = store.queue_assignment(
                        scenario, str(run_row["id"]), called_number
                    )
                    count += 1
                    print(
                        f"{count}\t{scenario.id}\t{assignment['id']}\t{called_number}"
                    )
            print(f"Queued {count} call(s) in run {run_row['id']}.")
            return 0
        if args.command == "score":
            settings = Settings.from_env(require_supabase=True)
            store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
            card = asyncio.run(
                score_attempt(store, args.attempt_id, args.model or settings.grader_model)
            )
            print(f"{args.attempt_id}\t{card.outcome}\t{card.summary}")
            for result in card.results:
                score_text = "-" if result.score is None else f"{result.score:.1f}"
                print(f"  {result.grader_id}@{result.grader_version}\t{result.status}\t{score_text}")
            return 0
        if args.command == "results":
            store = _store()
            attempts = store.recent_attempts(max(1, min(args.limit, 100)))
            grader_rows = store.grader_results_for_attempts(
                [str(item["id"]) for item in attempts]
            )
            by_attempt: dict[str, list[dict]] = {}
            for row in grader_rows:
                by_attempt.setdefault(str(row["call_attempt_id"]), []).append(dict(row))
            for attempt in attempts:
                attempt_id = str(attempt["id"])
                invariant = next(
                    (
                        row
                        for row in by_attempt.get(attempt_id, [])
                        if row["grader_id"] == "scenario.invariants"
                    ),
                    None,
                )
                grade = "unscored"
                if invariant:
                    value = invariant.get("score")
                    score_text = "-" if value is None else f"{float(value):.1f}"
                    grade = f"{invariant['status']}:{score_text}"
                error = str(attempt.get("error") or "").replace("\n", " ")
                print(
                    f"{attempt['started_at']}\t{attempt['test_case_id']}\t"
                    f"{attempt['status']}\t{attempt.get('outcome') or '-'}\t{grade}\t"
                    f"{attempt_id}\t{error}"
                )
            return 0
        if args.command == "error-log":
            store = _store()
            attempts = store.recent_attempts(max(1, min(args.limit, 200)))
            attempt_ids = [str(item["id"]) for item in attempts]
            assignment_ids = sorted({str(item["assignment_id"]) for item in attempts})
            report = build_error_log(
                attempts=attempts,
                assignments=store.assignments_for_ids(assignment_ids),
                findings=store.findings(),
                occurrences=store.finding_occurrences_for_attempts(attempt_ids),
                events=store.events_for_attempts(attempt_ids),
                artifacts=store.artifacts_for_attempts(attempt_ids),
            )
            write_error_log(args.output, report)
            summary = report["summary"]
            print(
                f"Error log: {summary['findings']} finding(s), "
                f"{summary['occurrences']} occurrence(s)."
            )
            print(args.output.resolve())
            return 0
        if args.command == "offline-export":
            store = _store()
            attempts = [
                item
                for item in store.recent_attempts(max(1, min(args.limit, 500)))
                if str(item["test_case_id"]).startswith(args.prefix)
                and item["status"] in {"completed", "failed"}
            ]
            latest = {}
            for attempt in attempts:
                latest.setdefault(str(attempt["test_case_id"]), attempt)
            selected = list(latest.values())
            grader_rows = store.grader_results_for_attempts(
                [str(item["id"]) for item in selected]
            )
            by_attempt: dict[str, list[dict]] = {}
            for row in grader_rows:
                by_attempt.setdefault(str(row["call_attempt_id"]), []).append(dict(row))
            for attempt in selected:
                attempt_id = str(attempt["id"])
                fixture = export_fixture(
                    attempt=attempt,
                    context=store.attempt_context(attempt_id),
                    events=store.events_for_attempt(attempt_id),
                    grader_results=by_attempt.get(attempt_id, []),
                )
                path = write_fixture(args.output, fixture)
                print(f"exported\t{attempt['test_case_id']}\t{path}")
            print(f"Exported {len(selected)} fixture(s) to {args.output}.")
            return 0
        if args.command == "offline-test":
            fixture_results = []
            for fixture in load_fixtures(args.fixtures):
                definition = fixture["scenario"]
                scenario = Scenario(
                    id=str(definition["id"]),
                    version=int(definition["version"]),
                    title=str(definition["title"]),
                    priority=str(definition["priority"]),
                    definition=definition,
                )
                findings = evaluate_transcript(scenario, fixture["events"])
                contradictions = grader_contradictions(
                    fixture.get("stored_outcome"),
                    fixture.get("grader_results") or [],
                    findings,
                )
                fixture_results.append(
                    {
                        "scenario_id": scenario.id,
                        "stored_outcome": fixture.get("stored_outcome"),
                        "findings": [finding_dict(item) for item in findings],
                        "grader_contradictions": [
                            finding_dict(item) for item in contradictions
                        ],
                    }
                )
                print(
                    f"replay\t{scenario.id}\tfindings={len(findings)}\t"
                    f"contradictions={len(contradictions)}"
                )
            mutation_results = []
            selected = [item for item in scenarios if item.id.startswith(args.prefix)]
            for scenario in selected:
                findings = evaluate_transcript(scenario, mutation_case(scenario))
                mutation_results.append(
                    {
                        "scenario_id": scenario.id,
                        "detected": bool(findings),
                        "findings": [finding_dict(item) for item in findings],
                    }
                )
                print(f"mutation\t{scenario.id}\t{'detected' if findings else 'MISSED'}")
            report = report_payload(fixture_results, mutation_results)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            summary = report["summary"]
            print(
                f"Offline report: {summary['fixtures']} fixture(s), "
                f"{summary['grader_contradictions']} grader contradiction(s), "
                f"{summary['mutations_detected']}/{summary['mutations']} mutations detected."
            )
            return 0 if summary["mutations_detected"] == summary["mutations"] else 2
        if args.command == "portal-personas":
            settings = Settings.from_env(require_supabase=False)
            portal = _signed_in_portal(settings)
            for persona in portal.personas():
                print(
                    f"{persona.get('id')}\t{persona.get('first_name')} "
                    f"{persona.get('last_name')}\t{persona.get('preferred_language')}"
                )
            return 0
        if args.command == "manual-call":
            settings = Settings.from_env(require_supabase=True)
            phone = normalize_phone(args.phone)
            if not is_e164(phone):
                raise ValueError("--phone must be a valid approved E.164 phone number")
            scenario = find_scenario(args.scenarios, args.scenario_id)
            persona_id = args.persona_id or settings.portal_persona_id
            if not persona_id:
                raise ValueError(
                    "Set WEDGE_PORTAL_PERSONA_ID or pass --persona-id; use portal-personas to list IDs"
                )
            portal = _signed_in_portal(settings)
            personas = {str(item.get("id")): item for item in portal.personas()}
            if persona_id not in personas:
                raise ValueError(f"Unknown portal persona: {persona_id}")
            store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
            store.upsert_test_case(scenario)
            run_row = store.create_run(args.label, args.agent_version, mode="manual")
            run_id = str(run_row["id"])
            assignment = store.queue_assignment(scenario, run_id, phone)
            assignment_id = str(assignment["id"])
            attempt_id: Optional[str] = None
            call_id: Optional[str] = None
            _print_manual_script(scenario)
            try:
                call_id = portal.start_call(
                    persona_id=persona_id,
                    language=scenario.portal_language,
                    phone=phone,
                )
                attempt_id = store.start_manual_attempt(assignment, call_id)
                print(f"Calling now. Portal call {call_id}; attempt {attempt_id}.", flush=True)
                call = portal.wait_for_end(call_id, timeout_seconds=args.timeout)
                store.ingest_portal_call(attempt_id, call)
                store.finish_attempt(assignment_id, attempt_id, run_id, "completed")
            except Exception as error:
                if attempt_id:
                    store.finish_attempt(
                        assignment_id, attempt_id, run_id, "failed", str(error)
                    )
                else:
                    store.fail_assignment(assignment_id, run_id, str(error))
                raise
            print(
                f"Saved manual call {call_id} as attempt {attempt_id}. "
                "Recording, transcript, outcome, and portal metadata were imported."
            )
            print("No LiveKit worker or automated grader was used.")
            return 0
        if args.command == "run-batch":
            if args.repeat < 1 or args.repeat > 20:
                raise ValueError("--repeat must be between 1 and 20")
            if args.concurrency < 1 or args.concurrency > 10:
                raise ValueError("--concurrency must be between 1 and 10")
            settings = Settings.from_env(require_supabase=True)
            called_number = normalize_phone(args.called_number or settings.called_number or "")
            if not is_e164(called_number):
                raise ValueError(
                    "Set QA_CALLED_NUMBER or pass --called-number in valid E.164 format"
                )
            persona_id = args.persona_id or settings.portal_persona_id
            if not persona_id:
                raise ValueError(
                    "Set WEDGE_PORTAL_PERSONA_ID or pass --persona-id; use portal-personas to list IDs"
                )
            selected = scenarios
            if args.scenario_ids:
                selected = [find_scenario(args.scenarios, item) for item in args.scenario_ids]
            portal = _signed_in_portal(settings)
            personas = {str(item.get("id")): item for item in portal.personas()}
            if persona_id not in personas:
                raise ValueError(f"Unknown portal persona: {persona_id}")
            store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
            store.upsert_test_cases(selected)
            run_row = store.create_run(args.label, args.agent_version)
            run_id = str(run_row["id"])
            total = len(selected) * args.repeat
            failures = 0
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                for scenario_index, scenario in enumerate(selected):
                    for wave_start in range(0, args.repeat, args.concurrency):
                        wave_size = min(args.concurrency, args.repeat - wave_start)
                        work = []
                        # A wave contains one scenario only. The inbound phone queue does
                        # not expose a portal call token, so mixing scenarios here could
                        # attach the wrong prompt when calls arrive out of order.
                        for wave_offset in range(wave_size):
                            repetition = wave_start + wave_offset
                            index = scenario_index * args.repeat + repetition + 1
                            assignment = store.queue_assignment(
                                scenario, run_id, called_number
                            )
                            future = executor.submit(
                                _run_portal_assignment,
                                settings,
                                scenario,
                                persona_id,
                                called_number,
                                assignment,
                                run_id,
                                args.timeout,
                            )
                            work.append((index, assignment, future))
                        by_future = {
                            future: (index, assignment)
                            for index, assignment, future in work
                        }
                        for future in as_completed(by_future):
                            index, assignment = by_future[future]
                            try:
                                result = future.result()
                                print(
                                    f"[{index}/{total}] {scenario.id}: {result['outcome']} - "
                                    f"{result['summary']}",
                                    flush=True,
                                )
                            except Exception as error:
                                failures += 1
                                print(
                                    f"[{index}/{total}] {scenario.id}: error - {error}",
                                    file=sys.stderr,
                                    flush=True,
                                )
            store.refresh_run_status(str(run_row["id"]))
            print(
                f"Completed portal batch run {run_row['id']} with {failures} runner error(s)."
            )
            return 2 if failures else 0
    except (PortalError, ScenarioValidationError, StoreError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 1


def _signed_in_portal(settings: Settings) -> WedgePortalClient:
    missing = []
    if not settings.portal_access_code:
        missing.append("WEDGE_PORTAL_ACCESS_CODE")
    if not settings.portal_tester_name:
        missing.append("WEDGE_PORTAL_TESTER_NAME")
    if not settings.portal_tester_email:
        missing.append("WEDGE_PORTAL_TESTER_EMAIL")
    if missing:
        raise ValueError("Set portal credentials in .env.local: " + ", ".join(missing))
    portal = WedgePortalClient(settings.portal_url)
    portal.sign_in(
        settings.portal_access_code,
        settings.portal_tester_name,
        settings.portal_tester_email,
    )
    return portal


def _print_manual_script(scenario: Scenario) -> None:
    print(f"Scenario: {scenario.title}")
    persona = scenario.definition.get("persona")
    if isinstance(persona, dict):
        print(
            "Persona: "
            + ", ".join(f"{key.replace('_', ' ')}={value}" for key, value in persona.items())
        )
    print("Caller instructions:")
    for instruction in scenario.definition.get("caller_policy", []):
        if not isinstance(instruction, dict):
            continue
        for trigger, text in instruction.items():
            print(f"  {trigger.replace('_', ' ')}: {text}")
    print("The phone call starts immediately after portal confirmation.", flush=True)


def _wait_for_attempt(
    store: SupabaseStore, assignment_id: str, timeout_seconds: int
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        attempt = store.attempt_for_assignment(assignment_id)
        if attempt is not None:
            last = dict(attempt)
            if attempt["status"] in {"completed", "failed"}:
                return last
        time.sleep(1.0)
    if last is not None:
        raise StoreError(
            f"Call attempt {last['id']} for assignment {assignment_id} remained "
            f"{last['status']} after {timeout_seconds}s"
        )
    raise StoreError(f"No call attempt appeared for assignment {assignment_id}")


def _run_portal_assignment(
    settings: Settings,
    scenario: Scenario,
    persona_id: str,
    called_number: str,
    assignment: Any,
    run_id: str,
    timeout_seconds: int,
) -> dict[str, str]:
    store = SupabaseStore(settings.supabase_url, settings.supabase_service_role_key)
    assignment_id = str(assignment["id"])
    call_id: Optional[str] = None
    try:
        portal = _signed_in_portal(settings)
        call_id = portal.start_call(
            persona_id=persona_id,
            language=scenario.portal_language,
            phone=called_number,
        )
        print(
            f"started {scenario.id}: portal {call_id}, assignment {assignment_id}",
            flush=True,
        )
        call = portal.wait_for_end(call_id, timeout_seconds=timeout_seconds)
        attempt = _wait_for_attempt(store, assignment_id, timeout_seconds=90)
        store.ingest_portal_call(str(attempt["id"]), call)
        card = asyncio.run(
            score_attempt(store, str(attempt["id"]), settings.grader_model)
        )
        return {"outcome": card.outcome, "summary": card.summary}
    except Exception as error:
        # If no worker ever claimed this item, remove it from the live queue so
        # it cannot contaminate a later scenario or batch.
        if store.attempt_for_assignment(assignment_id) is None:
            detail = f"Portal batch failed before an attempt completed: {error}"
            if call_id:
                detail += f" (portal call {call_id})"
            store.fail_assignment(assignment_id, run_id, detail)
        raise


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
