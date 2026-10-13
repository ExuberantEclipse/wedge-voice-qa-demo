# Wedge Voice Agent QA Harness

This repository contains the first runnable slice of the QA system described in
[`IMPLEMENTATION_SPEC.md`](./IMPLEMENTATION_SPEC.md): a scenario queue in Supabase and a
LiveKit worker that answers calls as a synthetic member.

The proposed secure inbound caller-number identification design is documented in
[`docs/proposals/caller-number-identification.md`](./docs/proposals/caller-number-identification.md).
The recommended real-phone implementation is the conversational Vapi demo in
[`docs/vapi-demo.md`](./docs/vapi-demo.md). The earlier deterministic Twilio proof is
retained in [`docs/twilio-demo.md`](./docs/twilio-demo.md).

## Demo review

- [Open the QA error log](./artifacts/error-log.html) — consolidated findings with
  severity, expected behavior, reproduction steps, recording links, and timestamps.
- The page currently contains 14 findings across 17 recorded occurrences. Download the
  HTML file from GitHub and open it locally if GitHub shows the source instead of the
  rendered page.

## How the call path works

```text
Wedge test portal -> Wedge outbound agent -> dedicated QA phone number
                                              |
                                              v
                                      LiveKit inbound trunk
                                              |
                                              v
                                  qa-simulated-caller worker
                                              |
                                              v
                                      Supabase run records
```

The Wedge agent still originates the call. The QA worker owns the receiving number and
plays the member selected by the queued scenario. It does not call or modify the Wedge
agent directly.

## What works

- Versioned YAML scenarios with local validation.
- An atomic Supabase queue, so concurrent inbound calls cannot claim the same scenario.
- Correlation using the dialed LiveKit SIP number and SIP call ID.
- A current LiveKit Agents 1.8 worker using LiveKit Inference for STT, LLM, and TTS.
- Run, assignment, attempt, event, grader, finding, and evidence tables.
- Final LiveKit transcript turns, per-turn timing, close reasons, and STT/LLM/TTS
  errors are persisted as normalized call events.
- A separate LiveKit Inference grader scores every scenario invariant, validates cited
  event IDs, and writes failed checks or scoring failures to the findings log.
- Batch queueing plus an authenticated portal runner with bounded concurrent waves.
- Safe failure behavior when no test is queued: the caller acts like a wrong number and
  does not reveal test instructions.

The authenticated batch runner can ingest the test portal's transcript, tool actions,
outcome, end reason, and recording reference when portal automation is authorized and
the local `WEDGE_PORTAL_*` values are configured. Credentials remain local.

## Prerequisites

- Python 3.10–3.14. This machine already has Python 3.11 at
  `/opt/homebrew/bin/python3.11`.
- A LiveKit Cloud project.
- A dedicated LiveKit phone number or third-party SIP DID.
- A Supabase project for synthetic QA data.

## Local setup

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env.local
```

Apply [`migrations/001_initial.sql`](./migrations/001_initial.sql) in the Supabase SQL
editor, then fill in `.env.local`. The service-role key is only used by this server-side
worker and CLI; never expose it to a frontend.

Validate the included scenarios:

```bash
.venv/bin/voice-qa validate
.venv/bin/voice-qa list
```

Seed all validated scenarios into Supabase (safe to rerun):

```bash
.venv/bin/voice-qa seed
```

## LiveKit telephony setup

In LiveKit Cloud:

1. Purchase a test phone number, or connect a third-party provider through an inbound
   SIP trunk.
2. Create an inbound dispatch rule for that trunk.
3. Dispatch calls to an agent session named `qa-simulated-caller`.
4. Put the DID in `QA_CALLED_NUMBER` using E.164 format.

LiveKit creates a SIP participant for each incoming call. The worker reads that
participant's `sip.trunkPhoneNumber` and `sip.callID` attributes, claims the oldest
matching queued scenario, and starts the synthetic member.

## Run one end-to-end test

Start the worker:

```bash
.venv/bin/voice-qa-worker dev
```

In a second terminal, queue a case:

```bash
.venv/bin/voice-qa queue identity_wrong_party_001
```

Then open the Wedge test portal, select the matching fictional member, enter the
dedicated QA phone number in **Call me on**, and start the call. Run cases sequentially
until a stronger correlation key is available from the originating system.

Inspect recent queue activity with:

```bash
.venv/bin/voice-qa queue-status
```

Inspect scored attempts:

```bash
.venv/bin/voice-qa results
.venv/bin/voice-qa score <attempt-id>
```

Build a single local review page containing every grader finding, its occurrences,
call metadata, cited evidence, grader verdict, and transcript:

```bash
.venv/bin/voice-qa error-log
```

Open `artifacts/error-log.html` and use the search, severity, status, and category
filters. The page is generated server-side; Supabase service credentials are never
embedded in it. Regenerate it after new calls are scored.

## Batch runs

Queue the full local catalog without originating calls:

```bash
.venv/bin/voice-qa batch-queue --agent-version <version>
```

For authorized portal automation, set `WEDGE_PORTAL_ACCESS_CODE`,
`WEDGE_PORTAL_TESTER_NAME`, `WEDGE_PORTAL_TESTER_EMAIL`, and
`WEDGE_PORTAL_PERSONA_ID` in `.env.local`. Verify the login and fictional persona IDs:

```bash
.venv/bin/voice-qa portal-personas
```

Run one call at a time with the default settings. Each call is queued immediately before
it is originated; the runner waits for completion, ingests portal artifacts, and reruns
scoring before starting the next call:

```bash
.venv/bin/voice-qa run-batch --repeat 1 --agent-version <version>
```

When no synthetic-caller minutes are available, a human tester can answer the approved
phone while the harness still automates evidence capture. This creates a manual
Supabase attempt, starts the portal call, waits for it to end, and imports the recording,
transcript, outcome, and metadata without invoking a LiveKit worker or automated grader:

```bash
.venv/bin/voice-qa manual-call manual_audio_background_noise_001 \
  --phone +1XXXXXXXXXX --agent-version <version>
```

The command prints the persona and caller instructions before starting the call.

To exercise a scenario several times at once, set a bounded concurrency. Concurrent
waves contain repetitions of one scenario only because the inbound DID does not expose
a portal correlation token:

```bash
.venv/bin/voice-qa run-batch --repeat 10 --concurrency 5 --agent-version <version>
```

## Credit-free regression testing

Completed calls can be replayed locally without starting LiveKit sessions. Exporting
keeps only redacted transcript turns, scenario metadata, and grader summaries:

```bash
.venv/bin/voice-qa offline-export --prefix matrix_
.venv/bin/voice-qa offline-test --prefix matrix_
```

The offline test applies deterministic consent, privacy, language, accessibility, and
workflow checks; detects contradictions where an LLM grader passed a deterministic
violation; and mutation-tests every selected scenario. The full JSON report is written
to `artifacts/offline-report.json`. This exercises policy and scoring logic but cannot
replace live audio checks for SIP routing, STT/TTS behavior, interruptions, or latency.

## Important limitations

- Critical/high automated findings remain candidates until a human verifies the cited
  transcript and recording evidence.
- A queued scenario is correlated by the dialed DID. With one DID, different scenarios
  run in separate waves; only repetitions of the same scenario run concurrently.
- The scenario prompt provides repeatability, not bit-for-bit identical speech. A later
  phase can add a deterministic scripted caller for exact regression tests.
- No real member data may be used. The system is for the portal's fictional members and
  approved tester numbers only.

## Next integration checkpoint

Once the LiveKit number and Supabase project exist, the smallest useful acceptance test
is:

1. Queue `identity_wrong_party_001`.
2. Start a portal call to the QA number.
3. Confirm the worker claims exactly one assignment.
4. Listen to the simulated family-member conversation.
5. Confirm the assignment and attempt are marked completed with the SIP call ID.
