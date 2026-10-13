# Wedge Voice Agent QA Demo

This demo shows how I tested a healthcare renewal voice agent, turned call failures into
reproducible evidence, and built a more reliable inbound voice experience around the
highest-risk cases I found.

The work has two connected parts:

1. A QA harness that runs realistic member scenarios and produces an evidence-backed
   error log.
2. A Vapi voice-agent demo that applies the findings to a safer, more deterministic
   Medi-Cal renewal conversation.

## Demo walkthrough

1. Start with the architecture and testing methodology below.
2. Open the [QA error log](./artifacts/error-log.html) and review a few recordings.
3. Return here for the Vapi voice-agent demo and the improvements it demonstrates.

> GitHub may display the HTML source instead of rendering the error log. Download the
> file and open it locally to use its search and filters.

## Architecture

### 1. Voice-agent testing and evidence collection

```text
Versioned scenarios
       |
       v
Supabase test queue 
       |                                           
       v                                           
LiveKit synthetic caller                           
       |                                           
       v                                           
Wedge outbound voice agent <--- Wedge test portal 
       |
       v
Recording + timestamped transcript + tool events
       |
       +-----------------------+
       |                       |
       v                       v
LLM invariant grader    Deterministic policy checks
       |                       |
       +-----------+-----------+
                   |
                   v
       Deduplicated QA error log
```

The test caller receives a queued persona, calls through the real voice path, and keeps
each run tied to its scenario. Supabase stores normalized call attempts, transcripts,
recording references, grader results, findings, and the exact occurrence of each issue.

The testing layer does not modify the agent under test. It observes the same call a
member would experience and preserves enough evidence for a reviewer to reproduce and
judge the failure.

### 2. Vapi improvement demo

```text
Member calls the demo number
            |
            v
     Vapi voice runtime
            |
            v
Authenticated Flask webhook
            |
     +------+------+
     |             |
     v             v
Identity guard   Renewal workflow controller
     |             |
     +------+------+
            |
            v
  Fictional member profile + call-scoped state
```

Vapi handles the live audio conversation. The local webhook controls identity state,
the next unanswered renewal question, profile updates, and callback consent. Important
decisions therefore do not depend on the language model remembering the conversation
correctly.

## How I tested the agent

### Scenario-driven calls

The scenario catalog covers the normal renewal path and adversarial branches such as:

- Callback requests, refusals, revocation, conditional consent, and being busy.
- Wrong numbers, family members, guardians, and other unverified callers.
- English/Spanish switching and explicit language corrections.
- Already-completed renewals, changed clinics, contact updates, and missing packets.
- Background noise, mishearing, interruptions, voicemail, and hearing difficulty.

Each scenario defines the fictional member, caller behavior, safety policy, and
invariants the agent must satisfy.

### Evidence-backed grading

Every completed call can preserve:

- The recording and exact error timestamp.
- The timestamped transcript and agent tool activity.
- What happened and what should have happened.
- Severity and issue category.
- Reproduction steps and repeated occurrences.

An LLM grader evaluates scenario-specific behavior, while deterministic checks enforce
rules that should not be left to grader interpretation—especially privacy, callback
consent, language preference, repeated workflow questions, and already-completed
renewals.

### Credit-free regression testing

Completed calls are exported as redacted transcript fixtures so policy logic can be
replayed without spending additional telephony or agent credits. Mutation tests inject
known violations and verify that the detection layer catches them.

The current repository test suite includes:

- **107 automated tests** across identity, workflow, grading, persistence, and provider
  adapters.
- **360 workflow combinations** covering core renewal branch permutations.
- **30/30 detected policy mutations** in the offline scenario report.

Audio-specific behavior such as latency, interruption timing, STT accuracy, and voice
quality still requires a live call.

## Results

The current [QA error log](./artifacts/error-log.html) contains **14 findings across 17
recorded occurrences**. It is deliberately organized for review rather than as raw
Supabase tables.

Representative findings include:

- Callback proposed without explicit consent.
- An unverified caller treated as authorized.
- Incorrect handling when a family member answers.
- The renewal script resumed after the member said renewal was complete.
- A text-only preference converted into a phone callback.
- Noisy speech accepted without an appropriate clarification.

Each entry contains only the review fields needed to make a decision: ID/title,
category, severity, recording and timestamp, actual behavior, expected behavior, and
reproduction steps.

## Vapi voice-agent demo

The Vapi demo is the proposed improvement path after reviewing the failures above. It
uses a fictional member record and guides the caller through a short Medi-Cal renewal
review instead of relying on a long scripted monologue.

### Golden path

1. Disclose the AI assistant and recording notice.
2. Ask whether the caller is the fictional member before sharing member details.
3. Explain why the Medi-Cal renewal review matters.
4. Check coverage status and current clinic.
5. Ask once about the renewal packet and whether help is needed.
6. Collect confirmed clinic, address, phone, or language changes one field at a time.
7. Offer a callback only when phone help is selected.
8. Summarize the result and tell the caller they can hang up.

### Improvements demonstrated

- **Server-directed sequencing:** one unanswered question and one caller response at a
  time, preventing the model from racing through the script.
- **Sticky verification state:** once the fictional caller is confirmed, the agent does
  not ask for verification again.
- **Wrong-party privacy boundary:** relatives and claimed representatives do not unlock
  member information.
- **Receptive refusals:** “no” closes the current branch without persuasion or a
  reworded second attempt.
- **Callback consent lock:** a declined or revoked callback cannot be offered again
  during that call.
- **Completed-renewal handling:** “already renewed” permanently skips the old packet and
  renewal-help loop.
- **Confirmed profile updates:** clinic and contact changes are read back and written
  only after confirmation.
- **Conversation recovery:** interruptions, ambiguous audio, language changes, and
  corrections preserve workflow state.
- **Safe completion:** the agent tells the caller the review is complete and lets the
  caller hang up instead of unexpectedly ending the call.

The implementation is in
[`src/voice_qa/vapi_demo.py`](./src/voice_qa/vapi_demo.py) and
[`src/voice_qa/renewal_workflow.py`](./src/voice_qa/renewal_workflow.py). The full local
setup and synthetic-data boundaries are documented in
[`docs/vapi-demo.md`](./docs/vapi-demo.md).

## Repository map

- [`artifacts/error-log.html`](./artifacts/error-log.html) — navigable findings and
  recording evidence.
- [`testcases/`](./testcases/) — versioned personas, edge cases, and safety invariants.
- [`fixtures/transcripts/`](./fixtures/transcripts/) — redacted offline replay fixtures.
- [`src/voice_qa/`](./src/voice_qa/) — test harness, graders, persistence, and Vapi demo.
- [`tests/`](./tests/) — automated regression and workflow-matrix tests.
- [`IMPLEMENTATION_SPEC.md`](./IMPLEMENTATION_SPEC.md) — deeper production-oriented
  design and future integration details.

## Safety boundary

This repository is a synthetic demonstration. It must not be used with real patient
information. Verbal confirmation in the Vapi flow unlocks only the fictional local
record; a production healthcare implementation requires an approved identity,
authorization, consent, retention, and audit model.
