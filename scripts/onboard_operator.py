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
    sheets_id = input("Google Sheets ID: ").strip()
    luganda_default = "Webale okutuukirira! Nnyinza okuyamba oluvannyuma."
    luganda = input(f"Luganda canned response [{luganda_default}]: ").strip()
    if not luganda:
        luganda = luganda_default

    # ── Validate inputs ──────────────────────────────────────────────
    errors = []
    if not channel_id:
        errors.append("Channel ID is required")
    if not channel_token:
        errors.append("Channel token is required")
    if not shop_name:
        errors.append("Shop name is required")
    if not owner_name:
        errors.append("Owner name is required")

    try:
        owner_phone = normalise(owner_phone_raw)
    except ValueError:
        errors.append(f"Invalid phone number: {owner_phone_raw!r}")
        owner_phone = ""

    if not sheets_id:
        errors.append("Google Sheets ID is required")

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
        data = resp.json()
        status_code = data.get("status", {}).get("code")
        if status_code != 4:
            print(
                "\nChannel is not connected. Make sure the operator has "
                "scanned the QR code in the Whapi dashboard before running "
                "this script.",
                file=sys.stderr,
            )
            print(f"Health status: {data.get('status', {})}", file=sys.stderr)
            raise SystemExit(1)
        print(f"  Channel connected: {data.get('user', {}).get('name', '?')}")
    except httpx.RequestError as e:
        print(f"\nCould not reach Whapi: {e}", file=sys.stderr)
        raise SystemExit(1)

    # ── Configure webhook ────────────────────────────────────────────
    webhook_secret = secrets.token_urlsafe(32)

    if not skip_webhook:
        server_url = cfg.server_url or os.getenv("SERVER_URL", "")
        if not server_url:
            server_url = input("Server URL (e.g. https://your-domain.com): ").strip()
        if not server_url:
            print("Server URL is required for webhook configuration", file=sys.stderr)
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
            if resp.status_code >= 400:
                print(f"\nFailed to configure webhook: {resp.text[:200]}", file=sys.stderr)
                raise SystemExit(1)
            print("  Webhook configured")
        except httpx.RequestError as e:
            print(f"\nCould not configure webhook: {e}", file=sys.stderr)
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
        luganda_canned_response=luganda,
        llm_model=cfg.llm_model,
        status=OperatorStatus.ACTIVE,
        created_at=datetime.utcnow(),
    )
    op_adapter.save(operator)
    print(f"  Operator {operator_id} created")

    # ── Load inventory ───────────────────────────────────────────────
    print("\nLoading inventory from Google Sheet...")
    if not cfg.google_credentials_json_b64:
        print("  Warning: GOOGLE_CREDENTIALS_JSON not set. Skipping inventory load.")
        product_count = 0
    else:
        try:
            loader = GoogleSheetsLoader(
                cfg.google_credentials_json_b64,
                sheets_id,
                cfg.google_sheet_name,
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
            # Get service account email for helpful error message
            try:
                import base64
                creds = json.loads(base64.b64decode(cfg.google_credentials_json_b64))
                email = creds.get("client_email", "unknown")
            except Exception:
                email = "the service account"

            print(
                f"\n  Could not load inventory. Check that the sheet is shared "
                f"with {email} as Viewer.\n"
                f"  Sheet ID used: {sheets_id}\n"
                f"  Error: {str(e)[:200]}",
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
    print(f"Salelular is now active on {shop_name}'s WhatsApp number.")
    print()
    print(
        f"{owner_name} will receive alerts on {owner_phone} "
        f"when customers are ready to buy."
    )


if __name__ == "__main__":
    main()
