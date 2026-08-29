#!/usr/bin/env python3
"""Slack Socket Mode listener for the PO review / invoicing slash commands.

/po-invoice <po_num> <shipment_id> is the main one: it runs the price check and
the shipment-quantity check, derives the invoice number and date, checks for
duplicates, and -- only if everything passes -- offers a button that creates the
Odoo draft invoice and (when enabled) the Spring invoice.

/po-review <po_num> and /po-ship-check <po_num> <shipment_id> run the pricing
and quantity checks individually, for debugging.

Every command needs the Camelot shipment_id looked up manually in Camelot's UI
-- there's no working PO#->shipment lookup for Target orders (
GetOrderStatusDateRange only ever returns DTC/TikTok shipments; see
camelot_client.py). That limitation is why /po-invoice takes two arguments.

None of these touch processed_pos.json, so they neither affect nor are affected
by the batch "new POs" flow in main.py.

This is the Slack adapter over workflow.py -- it parses command text and
renders results, while the Spring / Camelot / Odoo logic is shared with main.py.

Usage:
    python slack_listener.py
"""

import json
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import slack_notify
import workflow
from config import Config
from slack_notify import (
    format_invoice_preparation,
    format_invoicing_outcome,
    format_shipment_summary,
    format_summary,
)


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

    @app.command("/po-invoice")
    def handle_po_invoice(ack, command, say):
        ack()
        parts = command.get("text", "").strip().split()
        if len(parts) != 2:
            say(
                "Usage: `/po-invoice <po_num> <camelot_shipment_id>` -- e.g. "
                "`/po-invoice 10001993952-3840 S0461276`. Runs the price and quantity "
                "checks, then offers a button to create the invoices if both pass. "
                "The Camelot shipment ID has to be found manually in Camelot's UI."
            )
            return
        po_num, shipment_id = parts

        try:
            prep = workflow.prepare_invoice(clients, po_num, shipment_id)
            text, blocks = format_invoice_preparation(prep)
            say(text=text, blocks=blocks)
        except workflow.PONotFound:
            say(f":question: PO `{po_num}` not found.")
        except Exception as e:
            print(
                f"Error handling /po-invoice {po_num!r} {shipment_id!r}: {e}",
                file=sys.stderr,
            )
            say(f":rotating_light: Error preparing invoice for PO `{po_num}`: {e}")

    @app.action(slack_notify.INVOICE_CONFIRM_ACTION)
    def handle_invoice_confirm(ack, body, respond):
        ack()
        try:
            payload = json.loads(body["actions"][0]["value"])
            po_num = payload["po_num"]
            shipment_id = payload["shipment_id"]
        except (KeyError, IndexError, ValueError) as e:
            print(f"Malformed invoice-confirm payload: {e}", file=sys.stderr)
            respond(replace_original=False, text=":rotating_light: Couldn't read that button's data.")
            return

        user = body.get("user", {}).get("username") or body.get("user", {}).get("name", "someone")
        # Replace the original message straight away so the button is gone before
        # the slow work starts -- otherwise an impatient double-click invoices twice.
        respond(
            replace_original=True,
            text=f":hourglass_flowing_sand: Creating invoices for PO `{po_num}` (requested by {user})...",
        )

        try:
            prep = workflow.prepare_invoice(clients, po_num, shipment_id)

            # Re-derived from scratch rather than trusting the button payload: the
            # PO, the shipment or the price list may have changed since the check,
            # and the click may be hours old.
            if not prep.ready:
                text, blocks = format_invoice_preparation(prep)
                respond(
                    replace_original=True,
                    text=text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":no_entry: Nothing invoiced — PO `{po_num}` no longer passes its checks.",
                            },
                        },
                        *blocks,
                    ],
                )
                return

            if (
                prep.invoice_num != payload.get("invoice_num")
                or prep.invoice_date != payload.get("invoice_date")
            ):
                respond(
                    replace_original=True,
                    text=(
                        f":no_entry: Nothing invoiced — the derived invoice details changed "
                        f"since that check.\nWas `{payload.get('invoice_num')}` dated "
                        f"`{payload.get('invoice_date')}`, now `{prep.invoice_num}` dated "
                        f"`{prep.invoice_date}`.\nRe-run `/po-invoice {po_num} {shipment_id}`."
                    ),
                )
                return

            outcome = workflow.execute_invoicing(clients, prep)
            respond(
                replace_original=True,
                text=f"PO {po_num} invoiced as {outcome.invoice_num} (requested by {user})\n"
                + format_invoicing_outcome(outcome),
            )
        except Exception as e:
            print(f"Error invoicing {po_num!r} {shipment_id!r}: {e}", file=sys.stderr)
            respond(
                replace_original=True,
                text=(
                    f":rotating_light: Error invoicing PO `{po_num}`: {e}\n"
                    "Check Odoo and Spring before retrying -- part of it may have gone through."
                ),
            )

    SocketModeHandler(app, config.slack_app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
