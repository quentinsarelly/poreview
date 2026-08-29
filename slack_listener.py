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

This is the Slack adapter over workflow.py -- it parses command text and
renders results, while the Spring / Camelot logic is shared with main.py.

Usage:
    python slack_listener.py
"""

import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import workflow
from config import Config
from slack_notify import format_shipment_summary, format_summary


def main() -> int:
    config = Config.load()
    try:
        workflow.require_listener_env(config, "Set them in .env.")
    except workflow.WorkflowError as e:
        print(str(e), file=sys.stderr)
        return 1

    clients = workflow.Clients(config)
    app = App(token=config.slack_bot_token)

    @app.command("/po-review")
    def handle_po_review(ack, command, say):
        ack()
        po_num = command.get("text", "").strip()
        if not po_num:
            say("Usage: `/po-review <po_num>`")
            return

        try:
            po = workflow.fetch_po(clients, po_num)
            price_map = workflow.load_price_map(config)
            say(format_summary(workflow.review_pricing(po, price_map)))
        except workflow.PONotFound:
            say(f":question: PO `{po_num}` not found.")
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
            po = workflow.fetch_po(clients, po_num)
            result = workflow.check_shipment_quantities(clients, po, shipment_id)
            say(format_shipment_summary(result))
        except workflow.PONotFound:
            say(f":question: PO `{po_num}` not found.")
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
