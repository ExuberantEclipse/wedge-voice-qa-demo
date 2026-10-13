# Caller-number-assisted member identification

Status: Proposed prototype  
Date: 2026-10-12  
Risk level: High (identity, privacy, and member-specific workflow access)

## 1. Summary

When a person calls the Wedge inbound number, use the network-provided caller number to
locate a possible member and shorten the opening conversation. The phone-number match
is a routing hint, not proof of identity. Member-specific facts and tools remain locked
until a deterministic verification service approves the caller or their authority to
act for the member.

The proposed control point lives outside the language model:

```text
trusted telephony caller number
             |
             v
    candidate lookup service
       /       |        \
   no match  unique   multiple
       \        |        /
        generic conversation
             |
             v
 deterministic verification gate
      failed | verified | representative
             |
             v
 capability-scoped member context and tools
```

## 2. Problem

An inbound agent should not require every caller to explain which member they are
calling about when the incoming number already corresponds to a known contact. At the
same time, using caller ID as authentication would create serious failure modes:

- household members can share one number;
- phone numbers are reassigned or entered incorrectly;
- caller ID can be withheld or spoofed;
- a relative may possess the member's phone without authorization;
- one number can legitimately correspond to several members;
- revealing a member's name during the greeting can itself disclose a relationship.

The product should gain convenience from the number match without weakening the
identity gate.

## 3. Goals

- Find zero, one, or several candidate members from a trusted inbound caller number.
- Keep the interaction generic until identity or representative authority is verified.
- Prevent the LLM from bypassing verification through prompting or tool selection.
- Support shared, changed, blocked, malformed, and recycled numbers.
- Make every lookup, verification decision, and privileged tool call auditable.
- Allow the same core to run behind a browser demo, Twilio, Telnyx, or LiveKit.

## 4. Non-goals

- Treating possession of a phone number as proof of identity.
- Loading a complete member record into the model before verification.
- Defining Wedge's final identity-verification policy; compliance/product owners must
  approve the real factors and scripts.
- Using real member information in the prototype.
- Modifying production member contact information from the demo.
- Replacing the existing outbound-agent QA system.

## 5. Trust boundaries

### Trusted

- A validated telephony webhook or media connection.
- The server-side lookup and verification service.
- A server-side secret used to create phone lookup tokens.
- The authoritative member/contact and authorization systems.

### Untrusted or insufficient by itself

- Caller ID/ANI, even when delivered by a telephony provider.
- Anything stated in natural-language conversation.
- LLM claims about verification or authorization.
- Browser-supplied caller numbers outside the explicitly labeled demo environment.
- `user_metadata`, client-side flags, and model-generated tool arguments.

## 6. Conversation state machine

The application, not the LLM, owns the state.

| State | Meaning | Member-specific context | Permitted behavior |
|---|---|---:|---|
| `unidentified` | No trusted number or no match | None | Identify Wedge and ask the caller to identify their purpose/member generically |
| `candidate_unique` | One phone match | None | Ask the caller to provide approved verification factors without naming the candidate |
| `candidate_ambiguous` | Several matches | None | Ask who the caller is trying to reach; never enumerate matching members |
| `verification_pending` | Candidate chosen, factors incomplete | None | Collect only approved factors, apply attempt limits |
| `verified_member` | Member verification succeeded | Scoped member context | Answer and use member tools allowed by policy |
| `verified_representative` | Representative and authority verified | Authority-scoped context | Only actions covered by the authorization grant |
| `verification_failed` | Attempts exhausted or contradictory | None | Generic fallback, human handoff, or safe end |
| `locked` | Abuse/rate-limit threshold reached | None | No more factors; safe end and audit event |

State transitions must be explicit events. An LLM response cannot directly set
`verified_member` or `verified_representative`.

## 7. Inbound flow

1. The telephony adapter validates the provider signature before trusting the request.
2. It reads the provider-supplied caller number and normalizes it to E.164.
3. The server creates an HMAC lookup token using a server-only pepper. A plain SHA hash
   is insufficient because phone numbers have low entropy and are easy to enumerate.
4. The lookup service returns only `no_match`, `unique_candidate`, or
   `multiple_candidates`, plus opaque references. It does not return names, renewal
   status, coverage information, or dates.
5. The conversation controller selects the safe opening for that match status.
6. The caller provides the organization-approved verification factors.
7. The verification service compares factors server-side and returns a short-lived,
   call-bound capability token on success.
8. Member-context and action tools reject every request without a valid capability.
9. The token expires at call end or after a short TTL and cannot be reused on another
   call.

## 8. Safe opening behavior

### No match or blocked caller ID

> “Thanks for calling Wedge. I can help with your call, but first I need to understand
> who you're calling about.”

Do not say that the number is missing from the system.

### Unique candidate

> “Thanks for calling Wedge. Before I access any account information, I need to verify
> who I'm speaking with.”

Do not greet the candidate by name.

### Multiple candidates

> “Thanks for calling Wedge. Who are you calling about today?”

Do not enumerate names associated with the number.

### Representative

Collect the member claim and representative relationship, then verify the authorization
record. A claim such as “I'm their daughter” or “I'm their legal guardian” does not
change the state by itself.

## 9. Internal API contract

These APIs are server-to-server. They are not exposed directly to an anonymous browser
or to the LLM.

### Start identification

`POST /internal/calls/identify`

```json
{
  "provider_call_id": "opaque-provider-id",
  "caller_number": "+14155550123",
  "provider_signature_valid": true
}
```

```json
{
  "session_id": "opaque-session-id",
  "match_status": "unique_candidate",
  "candidate_handle": "opaque-candidate-handle",
  "verification_required": true
}
```

The response contains no member facts.

### Submit verification factors

`POST /internal/calls/{session_id}/verify`

```json
{
  "candidate_handle": "opaque-candidate-handle",
  "factors": {
    "claimed_name": "synthetic demo value",
    "approved_challenge": "synthetic demo value"
  }
}
```

```json
{
  "status": "verified_member",
  "capability_token": "short-lived-call-bound-token",
  "allowed_capabilities": ["read_renewal_summary", "request_human_handoff"]
}
```

Failure responses return only the remaining-attempt policy and safe next step. They do
not reveal which factor disagreed with the record.

### Retrieve scoped context

`POST /internal/member-context`

Requires the call-bound capability. The server returns only fields required for the
current intent. The LLM never performs an unrestricted member-table query.

## 10. Tool enforcement

Every member-specific tool uses a wrapper with this ordering:

1. Validate the call ID and capability token.
2. Validate token expiry, member scope, representative scope, and allowed action.
3. Validate tool arguments against server-side policy.
4. Execute the minimum necessary query/action.
5. Write an audit event containing the action and decision, without raw secrets.

Prompt instructions remain useful for conversation quality, but are not a security
control.

## 11. Proposed Supabase model

The prototype uses synthetic records only. Production integration should map these
concepts to Wedge's authoritative systems rather than automatically creating parallel
member data.

### `demo_members`

- `id uuid primary key`
- `display_label text` — synthetic demo label only
- `verification_secret_hash text`
- `status text`
- `synthetic_profile jsonb`

### `demo_member_phones`

- `id uuid primary key`
- `member_id uuid references demo_members`
- `phone_hmac text` — indexed equality token, never a plain hash
- `last_four text` — display/debugging only
- `active boolean`
- `verified_at timestamptz`
- unique index on `(member_id, phone_hmac)`; the same `phone_hmac` may belong to
  several members for shared-household tests

### `demo_identification_sessions`

- `id uuid primary key`
- `provider text`
- `provider_call_id text unique`
- `phone_hmac text`
- `match_status text`
- `state text`
- `candidate_member_id uuid null`
- `verification_attempts integer`
- `expires_at timestamptz`
- `created_at`, `verified_at`, `ended_at`

### `demo_verification_events`

- `id uuid primary key`
- `session_id uuid`
- `event_type text`
- `decision text`
- `reason_code text` — no raw factor value
- `metadata jsonb` — allowlisted fields only
- `created_at timestamptz`

### Access controls

- Put lookup and verification tables in a private schema where possible.
- Do not grant `anon` or `authenticated` direct table access.
- The browser demo calls a local server, never Supabase with a service-role key.
- If a privileged database function is eventually required, keep it out of `public`,
  revoke default `PUBLIC` execution, validate the authenticated service identity, and
  fix its `search_path`.
- Enable RLS as defense in depth even when tables are in a private schema.

The current Supabase changelog notes an upcoming retirement of outdated TLS 1.2 CBC
ciphers on November 10, 2026. The Python runtime used here supports modern TLS, but any
legacy production integration should be checked before rollout.

## 12. Privacy and abuse controls

- HMAC phone numbers with a versioned secret; support secret rotation.
- Store only the last four digits when a display hint is genuinely needed.
- Redact raw ANI from application logs and model prompts.
- Rate-limit by provider call, phone token, and network source.
- Use uniform failure messages so the verification flow cannot enumerate members.
- Apply a small verification-attempt limit and lock the session after exhaustion.
- Do not disclose whether a phone number or claimed name exists.
- Prevent capability tokens from being logged, replayed, or used across calls.
- Keep demo data synthetic and clearly labeled.
- Require privacy/security review before using real patient data.

## 13. Failure behavior

| Failure | Safe behavior |
|---|---|
| Missing/malformed caller number | Continue with generic identification |
| No match | Generic identification; do not reveal absence from records |
| Multiple matches | Ask caller to name the member; do not list candidates |
| Verification mismatch | Generic retry without identifying the mismatched factor |
| Attempts exhausted | Offer approved human path or safely end |
| Lookup/database unavailable | Apologize, remain generic, offer human path |
| Capability service unavailable | Block member-specific tools; fail closed |
| Caller claims representative status | Verify authorization before disclosure/action |
| Number changed/recycled | Verification fails safely; provide approved update path |

## 14. Acceptance criteria

### Privacy/security

- Zero member names or member-specific facts disclosed before verification across the
  canonical adversarial suite.
- Zero privileged tool calls succeed without a valid call-bound capability.
- A shared number never causes the agent to enumerate associated members.
- A family relationship claim never creates authorization on its own.
- Raw phone numbers and verification answers do not appear in model prompts or logs.

### Functional

- E.164 formatting variants map consistently.
- Zero, unique, and multiple-match branches produce the correct state.
- Successful verification unlocks only the intended member and capabilities.
- Call end revokes the capability.
- Lookup outages fail closed without ending the generic conversation unnecessarily.

### Experience

- Recognized callers complete verification with fewer turns than unmatched callers.
- The agent explains verification without claiming that caller ID proves identity.
- Failed verification offers an approved fallback without coercion.

## 15. Canonical test matrix

1. Unique number, correct member, successful verification.
2. Unique number, wrong person holding the phone.
3. Shared household number with two synthetic members.
4. Unknown number.
5. Withheld caller ID.
6. Same number in national and E.164 formatting.
7. Recycled number with an old inactive association.
8. Caller claims to be a daughter without authorization.
9. Verified legal representative with limited authority.
10. Correct first factor and incorrect second factor.
11. Repeated failed factors trigger lockout.
12. Lookup service unavailable.
13. Attempted member-context tool call before verification.
14. Capability replayed on a second call.
15. Prompt-injection attempt asking the model to mark the caller verified.

## 16. Demo plan without LiveKit

### Phase A — local browser demo (recommended first)

Build a local server and single-page interface with synthetic members. The presenter
selects or types a simulated incoming number and follows the conversation flow. The UI
shows two synchronized views:

- **Caller view:** only the safe messages and verification questions a caller would hear.
- **Control view:** match count, application-owned state, attempted transitions, allowed
  capabilities, and blocked tool calls.

Demo branches:

1. Unique match, successful verification, then scoped renewal context.
2. Wrong party on a uniquely matched number; member tools stay blocked.
3. Shared number; no names are enumerated.
4. Unauthorized family member; generic message only.
5. Unknown/blocked number; generic identification path.
6. Prompt-injection/tool-bypass attempt; gate blocks it.

This version requires no phone number, media provider, speech service, LLM, or cloud
deployment. It proves the product decision and security control directly. The core
state machine and tests are reused by every later adapter.

Estimated prototype effort: half a day for the deterministic core and test suite, then
half a day for a polished browser demo.

### Phase B — optional real-phone demo with Twilio

Twilio's official Media Streams interface supports bidirectional phone audio over a
secure WebSocket using `<Connect><Stream>`. The adapter would:

1. Receive an inbound call on a Twilio test number.
2. Validate `X-Twilio-Signature` before trusting the caller number.
3. Pass `From` into the same identification core.
4. Stream caller audio to the selected STT/LLM/TTS stack and audio back to Twilio.
5. Enforce member-tool capabilities in the same server-side gate.

This adds a public TLS/WSS endpoint, a Twilio account/number, usage charges, direct
speech/model credentials, audio interruption handling, and recording policy. It is a
more convincing phone demo but should follow—not precede—the deterministic prototype.

Estimated effort after Phase A: one day for a narrow happy-path phone demo, and another
one to two days for robust interruption, timeout, retry, and evidence handling.

### Telnyx alternative

Telnyx Programmable Voice/media streaming can fill the same adapter boundary and may be
attractive if Wedge already uses Telnyx. It still requires a number, signed webhooks,
public media endpoint, credentials, and an audio/AI pipeline. Do not select between
Twilio and Telnyx until account ownership, existing contracts, supported regions, and
recording requirements are known.

### Options not recommended for the first demo

- **Imitation LLM only:** fast, but it demonstrates prompt behavior rather than the
  security gate.
- **Self-hosted LiveKit:** avoids LiveKit Cloud agent credits but still requires SIP,
  inference providers, deployment, and more operations than the browser prototype.
- **Production member integration:** inappropriate before verification policy and data
  access are approved.

## 17. Implementation boundaries

Create these provider-independent components first:

- `PhoneNormalizer`
- `CandidateLookup`
- `IdentificationSession`
- `VerificationPolicy`
- `CapabilityIssuer`
- `MemberToolGate`
- `AuditSink`
- `TelephonyAdapter` interface

The browser, Twilio, Telnyx, and future Wedge adapters only translate input/output. They
must not own identity state or authorization decisions.

## 18. Rollout and rollback

1. Run the feature in shadow mode: calculate match status but do not alter conversation.
2. Compare candidate matching with human-reviewed test calls.
3. Enable the generic unique-candidate verification opening for synthetic/staff calls.
4. Verify zero pre-verification disclosures and measure added/removed turns.
5. Expand gradually behind a feature flag.
6. Roll back by disabling caller-number assistance; the generic verification flow
   remains available and no member tools become less protected.

## 19. Decisions required for production

1. Which factors satisfy member verification for each action risk level?
2. How is representative authority stored, scoped, and revoked?
3. Which pre-verification statements are approved as generic rather than member-specific?
4. What is the authoritative member/contact system and update process?
5. What are the attempt, lockout, retention, and human-handoff policies?
6. Does Wedge already have Twilio, Telnyx, or another inbound telephony contract?
7. Which production repository owns the inbound agent and member tools?

Until these are answered, implementation must remain synthetic and isolated from real
member records.
