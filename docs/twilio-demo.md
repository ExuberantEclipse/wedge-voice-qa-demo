# Twilio caller-identification demo

This is a real phone-call demo of caller-number-assisted member identification. It
uses Twilio `<Gather>` and `<Say>`, so it does not consume LiveKit minutes or require
another voice AI model. Caller ID only selects a candidate: the demo does not speak a
name or synthetic account detail until the caller enters the correct four-digit code.

Use synthetic data only. This prototype is not a production patient-verification
system and is not configured for PHI.

## 1. Configure local secrets

Add these server-side values to `.env.local`:

```dotenv
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PUBLIC_BASE_URL=https://your-public-https-host.example
TWILIO_DEMO_PEPPER=a-random-secret-containing-at-least-32-characters
TWILIO_DEMO_DATA_PATH=.twilio-demo/members.json
TWILIO_VALIDATE_SIGNATURES=true
```

Generate the pepper with a password manager or `openssl rand -hex 32`. Never expose
the auth token or pepper in browser code or commit them.

## 2. Seed one synthetic member

Use the phone that will call the Twilio number. The command prompts for the demo PIN
without putting it in shell history:

```bash
.venv/bin/voice-qa-twilio-demo seed \
  --phone +14255550123 \
  --member-id james-thornton \
  --name "James Thornton"
```

The local file contains keyed HMACs of the phone and PIN, not their raw values. It is
ignored by Git. Re-running the command for the same member ID replaces that member.

## 3. Expose the local webhook over HTTPS

Start an HTTPS tunnel to port 5000. For example, after installing Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:5000
```

Put the exact generated HTTPS origin in `TWILIO_PUBLIC_BASE_URL`, with no trailing
path. Then start the webhook server:

```bash
.venv/bin/voice-qa-twilio-demo serve
```

Confirm locally with `curl http://127.0.0.1:5000/healthz`.

## 4. Point a Twilio number at the demo

In Twilio Console, open the active phone number and set its incoming voice webhook:

- **A call comes in:** Webhook
- **URL:** `https://your-public-host.example/twilio/voice`
- **Method:** `POST`

If the number configuration exposes a call status callback, set it to
`https://your-public-host.example/twilio/status` with `POST`. The callback removes the
call-bound verification session when the call ends.

Call the Twilio number from the seeded phone. The expected path is:

1. Caller number produces a candidate but reveals no identity.
2. Caller enters the four-digit synthetic PIN.
3. Only after verification, the demo reads the synthetic name and renewal summary.
4. Three incorrect attempts lock the call.

An unknown number is asked to say the synthetic member's full name and must still
pass the PIN check. Shared household numbers follow the same name-selection path.

## Security boundary and next production step

The server validates `X-Twilio-Signature` against the exact public webhook URL and all
form parameters. Keep validation enabled outside unit tests. The candidate directory
and in-memory sessions are intentionally local for the demo; they do not change the
current Supabase schema.

For production, replace `DemoDirectory` with a server-side Supabase repository that
returns only candidate IDs, store verification attempts and decisions in an audit
table, use an approved identity factor rather than this demo PIN, and complete the
privacy/security review in
[`proposals/caller-number-identification.md`](./proposals/caller-number-identification.md).
