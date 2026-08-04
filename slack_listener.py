#!/usr/bin/env python3
"""Slack Socket Mode listener for the /po-review and /po-ship-check slash commands.

/po-review <po_num> fetches a PO from Spring Systems, price-checks it against
the Google Sheets price list, and posts the result back to the channel.

/po-ship-check <po_num> <shipment_id> checks a PO's ordered quantities against
what Camelot's WMS actually shipped. The Camelot shipment_id must be looked up
manually in Camelot's UI -- there's no working PO#->shipment lookup for Target
orders yet (GetOrderStatusDateRange only ever returns DTC/TikTok shipments;
see camelot_client.py).

Both are ad hoc, on-demand checks: neither touches processed_pos.json, so
neither affects (or is affected by) the batch "new POs" flow in main.py.

Usage:
    python slack_listener.py
"""

import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import price_list
from camelot_client import CamelotClient
from compare import evaluate_po
from compare_shipment import evaluate_shipment
from config import Config
from slack_notify import format_shipment_summary, format_summary
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
            ("CAMELOT_SOAP_URL", config.camelot_soap_url),
            ("CAMELOT_USERNAME", config.camelot_username),
            ("CAMELOT_PASSWORD", config.camelot_password),
            ("CAMELOT_CLIENT", config.camelot_client_code),
            ("CAMELOT_TRADING_PARTNER", config.camelot_trading_partner),
            ("CAMELOT_SHIPMENT_PROFILE", config.camelot_shipment_profile),
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
    camelot_client = CamelotClient(
        soap_url=config.camelot_soap_url,
        username=config.camelot_username,
        password=config.camelot_password,
        client_code=config.camelot_client_code,
        trading_partner=config.camelot_trading_partner,
        shipment_profile=config.camelot_shipment_profile,
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

    @app.command("/po-ship-check")
    def handle_po_ship_check(ack, command, say):
        ack()
        parts = command.get("text", "").strip().split()
        if len(parts) != 2:
            say(
                "Usage: `/po-ship-check <po_num> <camelot_shipment_id>` -- e.g. "
                "`/po-ship-check 10001964460-3841 S0461276`. The Camelot shipment ID "
                "has to be found manually in Camelot's UI for now."
            )
            return
        po_num, shipment_id = parts

        try:
            po = spring_client.get_po_by_num(po_num)
            if po is None:
                say(f":question: PO `{po_num}` not found.")
                return

            shipment = camelot_client.get_shipment_detail(shipment_id)
            result = evaluate_shipment(po, shipment)
            say(format_shipment_summary(result))
        except Exception as e:
            print(
                f"Error handling /po-ship-check {po_num!r} {shipment_id!r}: {e}",
                file=sys.stderr,
            )
            say(f":rotating_light: Error checking shipment `{shipment_id}` for PO `{po_num}`: {e}")

    SocketModeHandler(app, config.slack_app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
