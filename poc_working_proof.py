import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()

WHAPI_CHANNEL_TOKEN = os.getenv("WHAPI_CHANNEL_TOKEN", "")

app = FastAPI()


@app.get("/webhook")
async def webhook_liveness():
    return "OK"


@app.post("/webhook")
async def webhook_receive(request: Request):
    payload = await request.json()

    if payload.get("event", {}).get("type") != "messages":
        return {"status": "ignored"}

    for message in payload.get("messages", []):
        if message.get("from_me"):
            continue

        chat_id = message.get("chat_id", "")
        if chat_id.endswith("@g.us"):
            continue

        phone = message.get("from", "")
        text = message.get("text", {}).get("body", "")
        print(f"Received from {phone}: {text}")

        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://gate.whapi.cloud/messages/text?token={WHAPI_CHANNEL_TOKEN}",
                json={
                    "to": f"{phone}@s.whatsapp.net",
                    "body": "POC working. Received your message.",
                },
            )

    return {"status": "ok"}
