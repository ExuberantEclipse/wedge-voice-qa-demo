# Vapi secure inbound caller demo

This is the recommended real-phone demonstration. Vapi supplies the conversational
voice agent and an inbound US number; this repository supplies the caller-number
candidate lookup and synthetic-demo access boundary. It does not consume LiveKit
minutes or require a Twilio number.

Vapi currently offers one Vapi-managed US number before adding a payment method. The
number is inbound-only and free to provision, but calls and model usage consume Vapi
credits. Check the price shown in your account before testing.

Use synthetic data only. This streamlined Vapi demo intentionally has no keypad or
production-grade identity verification. Verbal confirmation unlocks only the fictional
local record and must never be used as the access rule for real patient information.

## 1. Create a Vapi account and private API key

Create or copy a private API key in the [Vapi Dashboard](https://dashboard.vapi.ai),
then put it in the existing blank field in `.env.local`:

```dotenv
VAPI_API_KEY=your_private_vapi_key
```

Do not paste the key into chat or commit `.env.local`.

Generate two different local secrets and add them as well:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

```dotenv
VAPI_WEBHOOK_SECRET=first_generated_value
VAPI_DEMO_PEPPER=second_generated_value
```

The webhook secret rejects requests that did not come through the configured Vapi
number. The pepper creates one-way HMAC lookup values for the caller number and PIN.

## 2. Seed one synthetic member

Use the personal phone that will call the Vapi number. The shared directory format still
requires a synthetic PIN for the separate Twilio adapter, but this Vapi flow never asks
for or uses it. Omit `--pin` so it is entered without appearing in shell history:

```bash
.venv/bin/voice-qa-vapi-demo seed \
  --phone +14255550123 \
  --member-id james-thornton \
  --name "James Thornton"
```

The resulting ignored local file stores keyed HMACs instead of the raw phone and PIN.

## 3. Start the webhook and HTTPS tunnel

In one terminal:

```bash
.venv/bin/voice-qa-vapi-demo serve
```

In another terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:5000
```

Copy the generated `https://...trycloudflare.com` origin into `.env.local` without a
trailing path:

```dotenv
VAPI_PUBLIC_BASE_URL=https://generated-host.trycloudflare.com
```

## 4. Create the free inbound number

Run:

```bash
.venv/bin/voice-qa-vapi-demo setup --area-code 425
```

The command creates a Vapi-managed inbound number with no fixed assistant. Incoming
calls are sent to the authenticated local webhook, which returns a transient assistant
based on the caller-number match state. The number ID is saved under `.vapi-demo/`, so
re-running setup updates the same number instead of requesting another one.

Call the printed number from the phone seeded in step 2. Expected behavior:

1. Caller ID finds the unique fictional candidate and asks, "Is this James?"
2. A verbal yes unlocks only the synthetic local record; there is no keypad step.
3. An unknown or shared caller number requires the exact synthetic full name before the
   same fictional-record confirmation.
4. The automatic renewal review then asks one server-selected question at a time.

The renewal workflow keeps the useful parts of the Wedge call: general Medi-Cal renewal
support, short packet guidance, and an offer of free phone or clinic help when requested.
It intentionally changes the interaction model:

- "Already renewed" stops the renewal path instead of restarting the packet script.
- "No" is accepted once, without persuasion or a reworded second attempt.
- Being busy never counts as callback consent. Callback requests must be explicit, and
  a refusal or revocation locks out later callback attempts during that call.
- Conditional consent remains conditional.
- A family relationship never substitutes for verification or authorization.
- The agent repairs uncertain audio with one short confirmation and switches languages
  when the caller does.
- Ordinary replies stay short, interruptions change the active path, and the agent does
  not return to a memorized monologue.

The renewal status, relevant workflow facts, and contact preference are held in
call-scoped memory. Contact-consent decisions are checked by deterministic server code;
the audit events store decisions and structured metadata, not the caller's raw words.

For the unique seeded caller number, the synthetic demo opens with "Is this James?" to
match the original outreach flow. Verbal confirmation is deliberately sufficient only
for this fictional record. A production implementation needs an approved identity factor
and should review whether even first-name disclosure is allowed.

After successful verification, the seeded James Thornton demonstration record includes
fictional date of birth, mailing address, county, languages, Medi-Cal status, primary-care
clinic and preference, renewal deadline, packet status, move status, authorized-contact
status, and support options. None of these fields are included in the assistant definition
or released before verification.

The agent automatically starts the renewal review after verification. It follows the
useful parts of the original Wedge flow—coverage changes, clinic preference, packet and
renewal help, address/contact changes, and the selected next step. It asks one question at
a time, skips answered items, and stops on refusal or after the relevant completed-renewal
branch.

For demo reliability, this sequence is server-directed rather than chosen freely by the
language model. Each recorded answer returns exactly one next step and one next question.
The model uses `gpt-4.1-mini` for intent recognition, natural acknowledgements, language
switching, and interruptions, but it cannot reorder or combine the core review questions.
The deterministic branches cover completed renewals, clinic changes, one or more profile
updates, phone or clinic help, callbacks, refusal, and wrong-party handling.

An explicit callback request opens a separate branch. The agent records consent first,
then collects only missing purpose, time window, timezone when needed, and language. The
server preserves the structured request and any condition or revocation. This local demo
does not schedule or place a real callback.

The verified caller can also update the fictional mailing address, contact phone,
primary-care clinic or preference, and preferred or secondary language. The agent must
read back one proposed value and receive explicit confirmation before the server accepts
the write. Confirmed changes persist in the ignored local `.vapi-demo/members.json` file
and are available on later calls. They do not alter the HMAC-based caller-number lookup,
Supabase, Medi-Cal, county records, an EHR, or any real clinic system.

Useful manual test prompts after entering the demo code are:

- "I already renewed."
- "No thanks, I don't need help."
- "I'm busy right now."
- "Do not call me back."
- "Only call me if the packet hasn't arrived by Friday."
- "Please walk me through my renewal."
- "What address do you have for me?"
- "Please call me tomorrow afternoon about my renewal packet."
- Start in Spanish, then say, "Can we continue in English?"
- Mumble a status, then correct the agent's interpretation.

For a presentation-friendly live transcript, open
**http://127.0.0.1:5000/demo/transcript** while the local demo server is running. It
updates automatically during the call and is deliberately blocked on the public
tunnel. Vapi's post-call transcripts and logs remain available under **Logs → Calls**
in the dashboard.

## Privacy boundary

The transient assistant prompt explicitly handles the previously observed failures: it
stops the renewal script when the member says they renewed, does not propose callbacks
after refusal, and follows the language the caller is currently using. These prompt
rules improve behavior, but server-side code—not the model—enforces identity access.

For a production implementation, replace the local directory with the reviewed
server-side Supabase repository, use Vapi Custom Credentials rather than an inline
demo header, configure the appropriate retention and HIPAA controls, and use an
approved identity factor rather than a transcript-visible PIN.
