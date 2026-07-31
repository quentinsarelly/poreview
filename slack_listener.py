#!/usr/bin/env python3
"""Slack Socket Mode listener for the /po-review slash command.

Lets anyone in the channel type `/po-review <po_num>` to fetch that specific PO
from Spring Systems, price-check it against the Google Sheets price list, and
post the result back to the channel -- without running the CLI by hand.

This is an ad hoc, on-demand check: it never touches processed_pos.json, so it
has no effect on (and isn't affected by) the batch "new POs" flow in main.py.

Usage:
    python slack_listener.py
"""

import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import price_list
from compare import evaluate_po
from config import Config
from slack_notify import format_summary
from spring_client import SpringSystemsClient


def main() -> int:
    config = Config.load()

    missing = [
        name
        for name, value in [
            ("SPRING_API_BASE_URL", config.spring_base_url),
            ("SPRING_API_USER", config.spring_api_user),
            ("SPRING_API_KEY", config.spring_api_key),
            ("SLACK_BOT_TOKEN", config.slack_bot_token),
            ("SLACK_APP_TOKEN", config.slack_app_token),
        ]
        if not value
    ]
    if missing:
        print(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in .env.",
            file=sys.stderr,
        )
        return 1

    spring_client = SpringSystemsClient(
        base_url=config.spring_base_url,
        api_user=config.spring_api_user,
        api_key=config.spring_api_key,
    )
    app = App(token=config.slack_bot_token)

    @app.command("/po-review")
    def handle_po_review(ack, command, say):
        ack()
        po_num = command.get("text", "").strip()
        if not po_num:
            say("Usage: `/po-review <po_num>`")
            return

        try:
            po = spring_client.get_po_by_num(po_num)
            if po is None:
                say(f":question: PO `{po_num}` not found.")
                return

            price_map = price_list.load_prices(
                sheet_id=config.google_sheet_id,
                credentials_path=config.google_credentials_path,
                token_path=config.google_token_path,
                worksheet_name=config.price_sheet_worksheet,
                sku_column=config.price_sheet_sku_column,
                price_column=config.price_sheet_price_column,
            )
            result = evaluate_po(po, price_map)
            say(format_summary(result))
        except Exception as e:
            print(f"Error handling /po-review {po_num!r}: {e}", file=sys.stderr)
            say(f":rotating_light: Error reviewing PO `{po_num}`: {e}")

    SocketModeHandler(app, config.slack_app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
