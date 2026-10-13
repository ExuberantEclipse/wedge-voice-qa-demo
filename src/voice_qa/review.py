"""Focused, self-contained error log for human QA review."""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def build_error_log(
    *,
    attempts: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Join Supabase rows into the seven fields required by the error log."""

    attempts_by_id = {str(item["id"]): dict(item) for item in attempts}
    assignments_by_id = {str(item["id"]): dict(item) for item in assignments}
    events_by_attempt = _group(events, "call_attempt_id")
    artifacts_by_attempt = _group(artifacts, "call_attempt_id")
    occurrences_by_finding = _group(occurrences, "finding_id")
    attempt_totals = Counter(str(item.get("test_case_id") or "") for item in attempts)

    issues = []
    for finding in findings:
        linked = []
        for occurrence in occurrences_by_finding.get(str(finding["id"]), []):
            attempt_id = str(occurrence["call_attempt_id"])
            attempt = attempts_by_id.get(attempt_id)
            if not attempt:
                continue
            assignment = assignments_by_id.get(str(attempt.get("assignment_id") or ""), {})
            raw_events = events_by_attempt.get(attempt_id, [])
            linked.append(
                {
                    **dict(occurrence),
                    "attempt": attempt,
                    "scenario": assignment.get("scenario_snapshot") or {},
                    "recording_url": _recording_url(
                        artifacts_by_attempt.get(attempt_id, []), raw_events
                    ),
                }
            )
        if not linked:
            continue
        issues.append(
            {
                **dict(finding),
                "short_title": _short_title(str(finding.get("title") or "Error")),
                "what_happened": _what_happened(finding, linked),
                "what_should_have_happened": _humanize(
                    str(finding.get("expected_behavior") or "")
                ),
                "recordings": [
                    {
                        "url": item.get("recording_url"),
                        "timestamp": _format_timestamp(item.get("offset_ms")),
                        "scenario_id": item["attempt"].get("test_case_id"),
                    }
                    for item in linked
                ],
                "how_to_reproduce": _reproduction(linked, attempt_totals),
                "occurrence_count": len(linked),
            }
        )

    issues.sort(
        key=lambda item: (
            SEVERITY_RANK.get(str(item.get("severity")), 99),
            str(item.get("short_title") or "").lower(),
        )
    )
    for index, issue in enumerate(issues, 1):
        issue["display_id"] = f"QA-{index:03d}"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issues": issues,
        "summary": {
            "findings": len(issues),
            "occurrences": sum(int(item["occurrence_count"]) for item in issues),
        },
    }


def write_error_log(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_error_log(report), encoding="utf-8")


def render_error_log(report: Mapping[str, Any]) -> str:
    issues = list(report.get("issues") or [])
    severities = _options(issues, "severity")
    categories = _options(issues, "category")
    cards = "\n".join(_issue_card(item) for item in issues)
    if not cards:
        cards = '<div class="empty">No grader-marked errors were found in this window.</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wedge QA error log</title>
<style>
:root {{ color-scheme:light; --ink:#17202a; --muted:#667085; --line:#d8dee8; --panel:#fff;
  --bg:#f4f7fb; --critical:#a70c2f; --high:#b54708; --medium:#175cd3; --low:#475467; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,sans-serif; }}
header {{ background:#101828; color:white; padding:30px max(24px,calc((100vw - 1050px)/2)); }}
h1 {{ margin:0 0 4px; font-size:30px; }} .subtitle {{ color:#cbd5e1; margin:0; }}
main {{ max-width:1050px; margin:auto; padding:24px; }}
.filters,.issue,.empty {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
.filters {{ padding:14px; display:grid; grid-template-columns:2fr 1fr 1fr; gap:10px; position:sticky; top:8px; z-index:5; box-shadow:0 4px 12px #1018280d; }}
input,select {{ width:100%; border:1px solid #b9c2cf; border-radius:8px; padding:10px; background:white; color:var(--ink); }}
.count {{ margin:12px 2px; color:var(--muted); }}
.issue {{ margin:14px 0; overflow:hidden; border-left:6px solid var(--low); }}
.issue[data-severity=critical] {{ border-left-color:var(--critical); }} .issue[data-severity=high] {{ border-left-color:var(--high); }} .issue[data-severity=medium] {{ border-left-color:var(--medium); }}
.issue-title {{ padding:18px 20px 12px; border-bottom:1px solid var(--line); }} .issue-title h2 {{ margin:0; font-size:21px; }}
.field {{ display:grid; grid-template-columns:190px 1fr; border-bottom:1px solid #edf0f4; }} .field:last-child {{ border-bottom:0; }}
.label {{ padding:13px 20px; color:var(--muted); font-size:12px; font-weight:750; letter-spacing:.05em; text-transform:uppercase; background:#fafbfc; }}
.value {{ padding:13px 20px; white-space:pre-line; }}
.badge {{ display:inline-block; border-radius:999px; background:#eef2f6; padding:3px 10px; font-size:12px; font-weight:700; }}
.critical {{ background:#ffe4e8; color:#8b0625; }} .high {{ background:#ffead5; color:#8f3104; }} .medium {{ background:#e8f1ff; color:#164c96; }}
.recording {{ display:block; margin-bottom:5px; }} .recording:last-child {{ margin-bottom:0; }}
.empty {{ padding:30px; text-align:center; color:var(--muted); }} .review-note {{ color:var(--muted); margin-top:18px; }}
@media (max-width:680px) {{ .filters,.field {{ grid-template-columns:1fr; }} .label {{ padding-bottom:4px; }} .value {{ padding-top:4px; }} }}
</style>
</head>
<body>
<header><h1>QA error log</h1><p class="subtitle">Focused evidence log: one situation per card</p></header>
<main>
 <section class="filters" aria-label="Error log filters">
  <input id="search" type="search" placeholder="Search ID, title, category, or behavior…" aria-label="Search">
  {_select('severity', 'Severity', severities)}
  {_select('category', 'Category', categories)}
 </section>
 <div id="visible-count" class="count"></div>
 <section id="issues">{cards}</section>
 <p class="review-note">Generated {_e(report.get('generated_at'))}. Critical/high automated findings require human validation against the linked recording.</p>
</main>
<script>
const controls = ['search','severity','category'].map(id => document.getElementById(id));
const cards = [...document.querySelectorAll('.issue')];
function applyFilters() {{
 const query=controls[0].value.trim().toLowerCase(); let visible=0;
 for (const card of cards) {{
  const show=(!query||card.dataset.search.includes(query))&&(!controls[1].value||card.dataset.severity===controls[1].value)&&(!controls[2].value||card.dataset.category===controls[2].value);
  card.hidden=!show; if(show) visible++;
 }}
 document.getElementById('visible-count').textContent=`${{visible}} of ${{cards.length}} errors shown`;
}}
controls.forEach(control=>control.addEventListener('input',applyFilters)); applyFilters();
</script>
</body></html>"""


def _issue_card(issue: Mapping[str, Any]) -> str:
    severity = str(issue.get("severity") or "unknown")
    category = _humanize(str(issue.get("category") or "uncategorized"))
    title = str(issue.get("short_title") or issue.get("title") or "Error")
    display_id = str(issue.get("display_id") or "QA-???")
    search = " ".join(
        str(issue.get(key) or "")
        for key in (
            "display_id",
            "short_title",
            "category",
            "severity",
            "what_happened",
            "what_should_have_happened",
            "how_to_reproduce",
        )
    ).lower()
    return f"""<article class="issue" data-severity="{_attr(severity)}" data-category="{_attr(issue.get('category'))}" data-search="{_attr(search)}">
 <div class="issue-title"><h2>{_e(display_id)} — {_e(title)}</h2></div>
 {_field('ID / title', f'{display_id} — {title}')}
 {_field('Category', category)}
 {_field_html('Severity', f'<span class="badge {_attr(severity)}">{_e(severity.title())}</span>')}
 {_field_html('Recording + timestamp', _recordings(issue.get('recordings') or []))}
 {_field('What happened', issue.get('what_happened'))}
 {_field('What should have happened', issue.get('what_should_have_happened'))}
 {_field('How to reproduce', issue.get('how_to_reproduce'))}
</article>"""


def _recordings(rows: Sequence[Mapping[str, Any]]) -> str:
    rendered = []
    for row in rows:
        timestamp = str(row.get("timestamp") or "00:00")
        scenario = str(row.get("scenario_id") or "call")
        url = str(row.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            content = f'<a href="{_attr(url)}" target="_blank" rel="noreferrer">Open recording</a> at {_e(timestamp)}'
        else:
            content = f'Recording unavailable · {_e(timestamp)}'
        rendered.append(f'<span class="recording">{content} · {_e(scenario)}</span>')
    return "".join(rendered) or "Recording unavailable"


def _what_happened(finding: Mapping[str, Any], linked: Sequence[Mapping[str, Any]]) -> str:
    parts = [str(finding.get("actual_behavior") or "").strip()]
    parts.extend(str(item.get("evidence_note") or "").strip() for item in linked)
    unique = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return "\n\n".join(unique)


def _reproduction(
    linked: Sequence[Mapping[str, Any]], attempt_totals: Counter[str]
) -> str:
    by_scenario = _group_occurrences(linked)
    sections = []
    for scenario_id, rows in by_scenario.items():
        scenario = rows[0].get("scenario") if isinstance(rows[0].get("scenario"), Mapping) else {}
        title = str(scenario.get("title") or scenario_id)
        persona = scenario.get("persona") if isinstance(scenario.get("persona"), Mapping) else {}
        persona_text = "; ".join(
            f"{_humanize(str(key))}: {value}"
            for key, value in persona.items()
            if value not in (None, "")
        ) or "Use the scenario's fictional member."
        policy = scenario.get("caller_policy")
        lines = []
        if isinstance(policy, list):
            for instruction in policy:
                if isinstance(instruction, Mapping):
                    for trigger, text in instruction.items():
                        lines.append(f"{_humanize(str(trigger))}: {text}")
        script = " | ".join(lines) or "Follow the stored caller policy."
        total = attempt_totals.get(scenario_id, 0)
        sections.append(
            f"{title}\nPersona: {persona_text}\nWhat to say: {script}\n"
            f"Observed: {len(rows)} of {total} stored attempt{'s' if total != 1 else ''}."
        )
    return "\n\n".join(sections)


def _recording_url(
    artifacts: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> str | None:
    for artifact in artifacts:
        if artifact.get("kind") == "recording" and artifact.get("uri"):
            return str(artifact["uri"])
    for event in events:
        if event.get("type") != "portal_call_ingested":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("recording_url"):
            return str(payload["recording_url"])
    return None


def _short_title(title: str) -> str:
    if title.startswith("Automated scoring failed:"):
        return "Automated scoring failed"
    if title.startswith("Incomplete or errored call evidence:"):
        return "Incomplete or errored call evidence"
    invariant = title.rsplit(":", 1)[-1].strip().lower().replace(" ", "_")
    known = {
        "correct_wrong_party_disposition": "Wrong-party disposition handled incorrectly",
        "no_false_claim_of_authorization": "Unverified caller treated as authorized",
        "no_callback_without_consent": "Callback proposed without consent",
        "text_preference_not_converted_to_callback": "Text preference converted into callback",
        "no_callback_before_explicit_consent": "Callback proposed before explicit consent",
    }
    return known.get(invariant, _humanize(invariant).capitalize())


def _group(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or ""), []).append(row)
    return grouped


def _group_occurrences(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        attempt = row.get("attempt") if isinstance(row.get("attempt"), Mapping) else {}
        grouped.setdefault(str(attempt.get("test_case_id") or "unknown"), []).append(row)
    return grouped


def _humanize(value: str) -> str:
    return " ".join(value.replace("_", " ").split())


def _format_timestamp(value: Any) -> str:
    try:
        total = max(0, int(value) // 1000)
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _field(label: str, value: Any) -> str:
    return f'<div class="field"><div class="label">{_e(label)}</div><div class="value">{_e(value)}</div></div>'


def _field_html(label: str, value: str) -> str:
    return f'<div class="field"><div class="label">{_e(label)}</div><div class="value">{value}</div></div>'


def _options(items: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    return sorted({str(item.get(key)) for item in items if item.get(key)})


def _select(identifier: str, label: str, options: Sequence[str]) -> str:
    choices = "".join(
        f'<option value="{_attr(item)}">{_e(_humanize(item))}</option>' for item in options
    )
    return f'<select id="{_attr(identifier)}" aria-label="{_attr(label)}"><option value="">All {_e(label.lower())}</option>{choices}</select>'


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _attr(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
