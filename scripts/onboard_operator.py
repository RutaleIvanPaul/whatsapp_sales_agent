"""Operator onboarding script.

Usage:
  python scripts/onboard_operator.py
  python scripts/onboard_operator.py --skip-webhook

Prerequisites:
  Developer creates channel in Whapi dashboard.
  Operator scans QR code via WhatsApp > Linked Devices.
  Channel shows as connected. Developer copies channel_id
  and channel_token from the dashboard.

This script handles system registration only — not QR/channel creation.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.adapters.inventory.cache import InventoryCache
from app.adapters.inventory.sheets import GoogleSheetsLoader, SheetsLoadError
from app.adapters.operator.sqlite_adapter import SqliteOperatorAdapter
from app.config import validate
from app.models.operator import Operator, OperatorStatus
from app.utils.phone import normalise
from datetime import datetime


def main():
    skip_webhook = "--skip-webhook" in sys.argv

    print("=" * 50)
    print("Salelular — Operator Onboarding")
    print("=" * 50)
    print()

    # ── Prompts ──────────────────────────────────────────────────────
    channel_id = input("Whapi channel ID: ").strip()
    channel_token = input("Whapi channel token: ").strip()
    shop_name = input("Shop name: ").strip()
    owner_name = input("Owner name: ").strip()
    owner_phone_raw = input("Owner personal WhatsApp number (e.g. +256...): ").strip()
    shop_category = input(
        "Shop category (e.g. 'Clothing & Fashion', 'Phone Accessories'): "
    ).strip()
    print(
        "Shop description — 1-3 sentences about what you sell, target customers, "
        "and how your product data is organised (gender/size tagging, categories, etc.).\n"
        "  Example: 'Sells women's and men's casual and formal wear. "
        "Products tagged by gender, size, colour.'"
    )
    shop_description = input("Shop description: ").strip()

    # ── Haggling strategy (optional) ────────────────────────────────
    print()
    print("───── Haggling (optional) ─────")
    print(
        "Customers often ask for discounts. You have three ways to shape\n"
        "how the bot handles this:\n"
        "\n"
        "  1. A shop-wide policy (next prompt) — 1-3 sentences the bot will\n"
        "     follow every time a customer haggles. Examples:\n"
        '       "Prices are fixed. No discounts."\n'
        '       "10% off purchases over 500k UGX. Buy 2, get 20% off the cheaper."\n'
        '       "Small discounts OK on clothing, fixed on electronics."\n'
        "\n"
        "  2. Per-product rules — optional 'haggling_notes' column in your\n"
        "     Google Sheet for item-specific overrides (e.g. 'Clearance, up\n"
        "     to 60% off', 'Firm — premium item'). Product rules override\n"
        "     the shop-wide policy.\n"
        "\n"
        "  3. Check-with-me mode (next prompt) — the bot notifies you before\n"
        "     responding to any haggling and waits for your decision. Your\n"
        "     policy and product notes are shown as context in the alert.\n"
        "\n"
        'Leaving everything blank means: "prices are fixed, decline politely."\n'
    )
    haggling_policy = input(
        "Haggling policy (1-3 sentences, or Enter to skip): "
    ).strip()
    haggling_notify_first_raw = input(
        "Check with you before responding to haggling? (y/N): "
    ).strip().lower()
    haggling_notify_first = haggling_notify_first_raw in ("y", "yes")
    if len(haggling_policy) > 500:
        print("  (policy truncated to 500 chars)")
        haggling_policy = haggling_policy[:500]

    sheets_id = input("Google Sheets ID (from sheet URL): ").strip()
    sheet_name_input = input("Sheet tab name [Sheet1]: ").strip()
    sheet_name = sheet_name_input or "Sheet1"
    luganda_default = "Webale okutuukirira! Nnyinza okuyamba oluvannyuma."
    luganda = input(f"Luganda canned response [{luganda_default}]: ").strip()
    if not luganda:
        luganda = luganda_default

    # ── Validate inputs ──────────────────────────────────────────────
    errors = []

    # Channel ID: must be non-empty, alphanumeric + hyphens, reasonable length
    if not channel_id:
        errors.append("Channel ID is required")
    elif not all(c.isalnum() or c in "-_" for c in channel_id):
        errors.append(
            f"Channel ID '{channel_id}' contains invalid characters. "
            f"It should be alphanumeric with hyphens (e.g. GROOTT-F552A). "
            f"Copy it exactly from the Whapi dashboard."
        )
    elif len(channel_id) < 5:
        errors.append(
            f"Channel ID '{channel_id}' looks too short. "
            f"Copy the full channel ID from the Whapi dashboard."
        )

    # Channel token: must be non-empty, reasonable length
    if not channel_token:
        errors.append("Channel token is required")
    elif len(channel_token) < 10:
        errors.append(
            f"Channel token looks too short ({len(channel_token)} chars). "
            f"Copy the full token from the Whapi dashboard."
        )

    if not shop_name:
        errors.append("Shop name is required")
    if not owner_name:
        errors.append("Owner name is required")
    if not shop_category:
        errors.append(
            "Shop category is required — used to help the bot understand "
            "what the shop sells (e.g. 'Clothing & Fashion')."
        )
    if not shop_description:
        errors.append(
            "Shop description is required — used to help the bot craft "
            "semantically relevant product searches."
        )

    try:
        owner_phone = normalise(owner_phone_raw)
    except ValueError:
        if not owner_phone_raw:
            errors.append("Owner phone number is required")
        elif not owner_phone_raw.startswith("+") and not owner_phone_raw.startswith("00"):
            errors.append(
                f"Phone number '{owner_phone_raw}' must include a country code "
                f"(e.g. +256700123456, not 0700123456)"
            )
        else:
            errors.append(
                f"'{owner_phone_raw}' doesn't look like a valid phone number. "
                f"Use the full number with country code, e.g. +256700123456"
            )
        owner_phone = ""

    if not sheets_id:
        errors.append("Google Sheets ID is required")
    elif len(sheets_id) < 20:
        errors.append(
            f"Google Sheets ID looks too short ({len(sheets_id)} chars). "
            f"It should be the long string from your sheet URL between /d/ and /edit"
        )
    elif " " in sheets_id or "\t" in sheets_id:
        errors.append(
            f"Google Sheets ID contains whitespace. "
            f"Copy the ID directly from the sheet URL — no spaces."
        )

    if errors:
        print("\nErrors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)

    # ── Load config ──────────────────────────────────────────────────
    cfg = validate()

    # ── Verify channel is connected ──────────────────────────────────
    print("\nVerifying channel connection...")
    try:
        resp = httpx.get(
            f"https://gate.whapi.cloud/health?token={channel_token}",
            timeout=10,
        )

        # Handle HTTP-level errors first (wrong token, bad request, etc.)
        if resp.status_code == 401 or resp.status_code == 403:
            print(
                "\nInvalid channel token. The token was rejected by Whapi.",
                file=sys.stderr,
            )
            print(
                "Check that you copied the full token from the Whapi dashboard "
                "(Settings → API → Token).",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if resp.status_code == 404:
            print(
                "\nChannel not found on Whapi. Check the channel token is correct.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if resp.status_code >= 400:
            print(
                f"\nWhapi returned HTTP {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        # Parse JSON response
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            print(
                f"\nUnexpected response from Whapi (not valid JSON). "
                f"Response: {resp.text[:200]}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        status_code = data.get("status", {}).get("code")
        status_text = data.get("status", {}).get("text", "unknown")

        if status_code != 4:
            print(
                f"\nChannel is not connected (status: {status_text}).",
                file=sys.stderr,
            )
            print(
                "Make sure the operator has scanned the QR code in the "
                "Whapi dashboard and their WhatsApp shows 'Linked Devices'.",
                file=sys.stderr,
            )
            if status_code == 1:
                print("  Hint: channel is starting up — wait a moment and try again.", file=sys.stderr)
            elif status_code == 2:
                print("  Hint: QR code is waiting to be scanned.", file=sys.stderr)
            elif status_code == 3:
                print("  Hint: channel is loading — wait a moment and try again.", file=sys.stderr)
            raise SystemExit(1)

        whapi_user = data.get("user", {}).get("name", "?")
        whapi_phone = data.get("user", {}).get("phone", "?")
        print(f"  Channel connected: {whapi_user} ({whapi_phone})")
    except SystemExit:
        raise
    except httpx.RequestError as e:
        print(
            f"\nCould not reach Whapi ({type(e).__name__}). "
            f"Check your internet connection.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # ── Configure webhook ────────────────────────────────────────────
    webhook_secret = secrets.token_urlsafe(32)

    if not skip_webhook:
        server_url = cfg.server_url or os.getenv("SERVER_URL", "")
        if not server_url:
            print(
                "\nSERVER_URL is not set in your .env file.\n"
                "  This is a deployment-level setting — not per-operator.\n"
                "  Add to .env: SERVER_URL=https://your-domain.com\n"
                "  Or pass --skip-webhook to configure the webhook manually later.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not server_url.startswith("https://"):
            if server_url.startswith("http://"):
                print(
                    "\nWarning: Server URL uses http:// — Whapi requires https:// "
                    "for webhook delivery. Use an ngrok or similar HTTPS tunnel.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            else:
                print(
                    f"\nServer URL '{server_url}' must start with https:// "
                    f"(e.g. https://your-domain.ngrok.io)",
                    file=sys.stderr,
                )
                raise SystemExit(1)

        webhook_url = f"{server_url.rstrip('/')}/webhook"
        print(f"\nConfiguring webhook → {webhook_url}")

        try:
            resp = httpx.patch(
                f"https://gate.whapi.cloud/settings?token={channel_token}",
                json={
                    "webhooks": [{
                        "url": webhook_url,
                        "events": [
                            {"type": "messages", "method": "post"},
                            {"type": "users", "method": "post"},
                            {"type": "users", "method": "delete"},
                        ],
                        "headers": {"X-Salelular-Token": webhook_secret},
                        "mode": "body",
                    }],
                    "media": {"auto_download": ["image", "document"]},
                    "callback_persist": True,
                    "sent_status": True,
                },
                timeout=15,
            )
            if resp.status_code == 401 or resp.status_code == 403:
                print(
                    "\nWebhook configuration failed: channel token was rejected.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if resp.status_code == 400:
                print(
                    f"\nWebhook configuration rejected by Whapi (bad request).\n"
                    f"  Response: {resp.text[:300]}",
                    file=sys.stderr,
                )
                if "url" in resp.text.lower():
                    print(
                        f"  Hint: check your server URL is a valid HTTPS address.",
                        file=sys.stderr,
                    )
                raise SystemExit(1)
            if resp.status_code >= 400:
                print(
                    f"\nWebhook configuration failed (HTTP {resp.status_code}).\n"
                    f"  Response: {resp.text[:300]}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            print("  Webhook configured")
        except SystemExit:
            raise
        except httpx.RequestError as e:
            print(
                f"\nCould not reach Whapi to configure webhook ({type(e).__name__}). "
                f"Check your internet connection.",
                file=sys.stderr,
            )
            raise SystemExit(1)
    else:
        print("\nSkipping webhook configuration (--skip-webhook)")

    # ── Create operator record ───────────────────────────────────────
    print("\nCreating operator record...")
    op_adapter = SqliteOperatorAdapter(cfg.storage_db_path, cfg.encryption_key)

    # Check for existing operator with same channel
    existing = op_adapter.get_by_channel_id(channel_id)
    if existing:
        print(f"  Operator with channel_id={channel_id} already exists. Overwriting.")
        op_adapter._conn.execute(
            "DELETE FROM operators WHERE channel_id=?", (channel_id,)
        )
        op_adapter._conn.commit()

    operator_id = f"op-{secrets.token_hex(4)}"
    operator = Operator(
        operator_id=operator_id,
        shop_name=shop_name,
        owner_name=owner_name,
        owner_personal_phone=owner_phone,
        whapi_channel_id=channel_id,
        whapi_channel_token=channel_token,
        whapi_webhook_secret=webhook_secret,
        whapi_connected_phone=None,
        google_sheets_id=sheets_id,
        google_sheet_name=sheet_name,
        luganda_canned_response=luganda,
        llm_model=cfg.llm_model,
        status=OperatorStatus.ACTIVE,
        created_at=datetime.utcnow(),
        shop_category=shop_category,
        shop_description=shop_description,
        haggling_policy=haggling_policy,
        haggling_notify_first=haggling_notify_first,
    )
    op_adapter.save(operator)
    print(f"  Operator {operator_id} created")

    # ── Load inventory ───────────────────────────────────────────────
    # ── Validate sheet tab name ───────────────────────────────────────
    print("\nVerifying Google Sheet access...")
    if cfg.google_credentials_json_b64 and sheets_id:
        try:
            import base64 as _b64
            creds_json = json.loads(_b64.b64decode(cfg.google_credentials_json_b64))
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build as gbuild
            from googleapiclient.errors import HttpError as _HttpError
            creds = Credentials.from_service_account_info(
                creds_json,
                scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
            )
            service = gbuild("sheets", "v4", credentials=creds, cache_discovery=False)
            meta = service.spreadsheets().get(spreadsheetId=sheets_id).execute()
            available_tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
            if sheet_name not in available_tabs:
                print(
                    f"\n  Tab '{sheet_name}' not found in the sheet.",
                    file=sys.stderr,
                )
                print(f"  Available tabs: {', '.join(available_tabs)}", file=sys.stderr)
                print(
                    f"  Note: tab names are case-sensitive.",
                    file=sys.stderr,
                )
                # Offer to pick the right one
                if len(available_tabs) == 1:
                    sheet_name = available_tabs[0]
                    print(f"  Using '{sheet_name}' (only tab available).")
                    # Update operator record with corrected tab name
                    operator.google_sheet_name = sheet_name
                    op_adapter.save(operator)
                else:
                    # Clean up the operator record before exiting
                    op_adapter._conn.execute(
                        "DELETE FROM operators WHERE operator_id=?", (operator_id,)
                    )
                    op_adapter._conn.commit()
                    raise SystemExit(1)
            else:
                print(f"  Sheet tab '{sheet_name}' found.")
        except SystemExit:
            raise
        except _HttpError as e:
            status = e.resp.status if e.resp else 0
            if status == 403:
                print(
                    f"\n  Permission denied for Google Sheet.\n"
                    f"  The sheet must be shared with the service account as Viewer.\n"
                    f"  Service account email is in your GOOGLE_CREDENTIALS_JSON.",
                    file=sys.stderr,
                )
            elif status == 404:
                print(
                    f"\n  Google Sheet not found (ID: {sheets_id}).\n"
                    f"  Check the Sheet ID — it's the long string from the "
                    f"sheet URL between /d/ and /edit.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"\n  Google Sheets API error (HTTP {status}): {str(e)[:200]}",
                    file=sys.stderr,
                )
            op_adapter._conn.execute(
                "DELETE FROM operators WHERE operator_id=?", (operator_id,)
            )
            op_adapter._conn.commit()
            print("  Operator record removed.", file=sys.stderr)
            raise SystemExit(1)
        except Exception as e:
            print(f"  Warning: could not verify sheet access ({type(e).__name__}: {e}). Continuing...")

    print("\nLoading inventory from Google Sheet...")
    if not cfg.google_credentials_json_b64:
        print("  Warning: GOOGLE_CREDENTIALS_JSON not set. Skipping inventory load.")
        product_count = 0
    else:
        try:
            loader = GoogleSheetsLoader(
                cfg.google_credentials_json_b64,
                sheets_id,
                sheet_name,
            )
            products = asyncio.run(loader.load())
            product_count = len(products)
            if product_count == 0:
                print(
                    "\n  Warning: No products loaded. Check that the sheet "
                    "has data in the expected columns.",
                )
            else:
                print(f"  Loaded {product_count} products")
        except SheetsLoadError as e:
            print(
                f"\n  Could not load inventory.\n"
                f"  {str(e)}\n"
                f"  Sheet ID: {sheets_id}\n"
                f"  Tab name: {sheet_name}",
                file=sys.stderr,
            )
            # Clean up — all or nothing
            op_adapter._conn.execute(
                "DELETE FROM operators WHERE operator_id=?", (operator_id,)
            )
            op_adapter._conn.commit()
            print("\n  Operator record removed (all-or-nothing).", file=sys.stderr)
            raise SystemExit(1)

    # ── Confirmation ─────────────────────────────────────────────────
    print()
    print("─" * 50)
    print(f"  Operator:          {shop_name}")
    print(f"  Owner:             {owner_name}")
    print(f"  Channel:           {channel_id}")
    print(f"  Products loaded:   {product_count}")
    print(f"  Webhook configured: {'yes' if not skip_webhook else 'skipped'}")
    print(f"  Status:            LIVE")
    print("─" * 50)
    print()
    print("Haggling strategy:")
    print(f"  Policy:            {haggling_policy or '(default: prices are fixed, decline politely)'}")
    print(f"  Check with you:    {'yes' if haggling_notify_first else 'no'}")
    print(f"  Per-product rules: add a 'haggling_notes' column to your sheet")
    print(f"                     any time — the bot reads it automatically")
    print()
    print(f"Salelular is now active on {shop_name}'s WhatsApp number.")
    print()
    print(
        f"{owner_name} will receive alerts on {owner_phone} "
        f"when customers are ready to buy."
    )


if __name__ == "__main__":
    main()
