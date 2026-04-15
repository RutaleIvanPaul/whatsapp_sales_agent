# Phase 0 — Proof of Concept

## Before writing any code: Whapi account setup

You need this done first or the POC cannot be tested.

1. Go to whapi.cloud and create a free account.
2. Start a trial or sandbox plan. The sandbox has limits (5 conversations/month,
   150 messages/day) which is enough for testing.
3. From the dashboard, find your API token and Project ID. Add to .env:
     WHAPI_PARTNER_TOKEN=your_token_here
     WHAPI_PROJECT_ID=your_project_id_here
4. Create a channel manually in the Whapi dashboard (or via API):
   - Click "Add Channel"
   - Note the channel ID and channel token that are returned.
   - Add to .env:
       WHAPI_CHANNEL_TOKEN=your_channel_token_here
       WHAPI_CHANNEL_ID=your_channel_id_here
5. Configure the channel webhook URL. You need a public URL.
   For local development: install ngrok and run `ngrok http 8000`.
   Copy the https URL (e.g. https://abc123.ngrok.io).
   In Whapi dashboard, set webhook URL to: https://abc123.ngrok.io/webhook
   Set events: messages only.
   Leave auth/headers empty for now (POC skips token validation).
6. Scan the QR code in Whapi dashboard using WhatsApp on your phone:
   Go to WhatsApp > Settings > Linked Devices > Link a Device > Scan QR.
7. Your number is now connected. Whapi will forward incoming messages to your server.

---

## What to build

A single file: poc.py in the project root.

This file does exactly one thing:
  Customer sends a WhatsApp message to your number.
  Your server receives the webhook.
  Your server sends a hardcoded reply back from the same number.

Nothing else. No sessions. No LLM. No database. No abstractions.

---

## What poc.py must contain

1. A FastAPI app with two routes:
   GET  /webhook  → returns 200 with "OK" (liveness)
   POST /webhook  → receives Whapi payload, sends reply

2. On POST /webhook:
   - Parse the JSON body
   - Check event.type == "messages" and from_me == false
   - Extract sender phone from messages[0].from
   - Extract message text from messages[0].text.body
   - Print to console: f"Received from {phone}: {text}"
   - Call Whapi send text endpoint to reply
   - Return 200

3. Whapi send text call:
   POST https://gate.whapi.cloud/messages/text?token={WHAPI_CHANNEL_TOKEN}
   Body: { "to": f"{phone}@s.whatsapp.net", "body": "POC working. Received your message." }

4. Load WHAPI_CHANNEL_TOKEN from environment variable (python-dotenv).

5. Run with: uvicorn poc:app --reload --port 8000

---

## What to add to .env for POC

  WHAPI_CHANNEL_TOKEN=your_channel_token
  WHAPI_CHANNEL_ID=your_channel_id

---

## What to add to requirements.txt for POC

  fastapi
  uvicorn[standard]
  httpx
  python-dotenv

---

## Success criteria

POC passes when:
  1. Server starts without errors
  2. ngrok is running and forwarding to port 8000
  3. Webhook URL is configured in Whapi dashboard
  4. You send a WhatsApp message to your connected number
  5. You receive a reply on the same WhatsApp number:
     "POC working. Received your message."
  6. Console shows the received message logged

When all five are true: POC is done. Delete poc.py. Advance to Phase 1.

---

## How to use this prompt

Paste this into Claude Code:

  Read CLAUDE.md fully.
  Then read .claude/prompts/poc.md fully.
  Before writing any code, tell me the exact structure of poc.py
  you plan to create and confirm the Whapi send API call format.
  Wait for my approval before writing anything.
  Use Plan Mode.
