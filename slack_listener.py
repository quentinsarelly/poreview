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

Slack requires an ack within 3 seconds, but these commands make several
sequential API calls (Spring, Camelot SOAP, Google Sheets, Odoo XML-RPC) and
can take far longer than that. So every handler acks immediately, posts an
ephemeral "working on it" note, and does the real work on a background thread --
which also stops slow commands from occupying Bolt's worker pool.

Usage:
    python slack_listener.py
"""

import json
import logging
import os
import sys
import threading
import traceback

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


def _in_background(label: str, fn) -> None:
    """Run fn on a daemon thread, logging anything it raises.

    Daemon so a shutdown doesn't hang on in-flight work; the tradeoff is that
    work in progress is lost on restart, which is fine here because every
    command is re-runnable and nothing is committed until the confirm button.
    """

    def runner() -> None:
        try:
            fn()
        except Exception:
            print(f"Unhandled error in {label}:", file=sys.stderr)
            traceback.print_exc()

    threading.Thread(target=runner, name=label, daemon=True).start()


def _configure_logging() -> None:
    """Without this, Bolt's logging goes nowhere and a deployed listener
    produces an empty log -- including the 'session established' line that is
    the only confirmation the websocket actually connected. Unbuffered stdout
    so lines appear in `railway logs` as they happen rather than in blocks."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    _configure_logging()
    config = Config.load()
    try:
        workflow.require_listener_env(config, "Set them in .env.")
    except workflow.WorkflowError as e:
        print(str(e), file=sys.stderr)
        return 1

    clients = workflow.Clients(config)
    app = App(token=config.slack_bot_token)

    @app.command("/po-review")
    def handle_po_review(ack, command, say, respond):
        ack()
        po_num = command.get("text", "").strip()
        if not po_num:
            say("Usage: `/po-review <po_num>`")
            return
        respond(f":hourglass_flowing_sand: Reviewing PO `{po_num}`...")

        def work() -> None:
            try:
                po = workflow.fetch_po(clients, po_num)
                say(format_summary(workflow.review_pricing(po, clients.price_map())))
            except workflow.PONotFound:
                say(f":question: PO `{po_num}` not found.")
            except Exception as e:
                print(f"Error handling /po-review {po_num!r}: {e}", file=sys.stderr)
                say(f":rotating_light: Error reviewing PO `{po_num}`: {e}")

        _in_background(f"po-review {po_num}", work)

    @app.command("/po-ship-check")
    def handle_po_ship_check(ack, command, say, respond):
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
        respond(f":hourglass_flowing_sand: Checking shipment `{shipment_id}` for PO `{po_num}`...")

        def work() -> None:
            try:
                po = workflow.fetch_po(clients, po_num)
                result = workflow.check_shipment_quantities(clients, po, shipment_id)
                say(format_shipment_summary(result))
            except workflow.PONotFound:
                say(f":question: PO `{po_num}` not found.")
            except workflow.WorkflowError as e:
                say(f":warning: {e}")
            except Exception as e:
                print(
                    f"Error handling /po-ship-check {po_num!r} {shipment_id!r}: {e}",
                    file=sys.stderr,
                )
                say(f":rotating_light: Error checking shipment `{shipment_id}` for PO `{po_num}`: {e}")

        _in_background(f"po-ship-check {po_num}", work)

    @app.command("/po-invoice")
    def handle_po_invoice(ack, command, say, respond):
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
        respond(
            f":hourglass_flowing_sand: Checking PO `{po_num}` against shipment "
            f"`{shipment_id}` — this takes a few seconds..."
        )

        def work() -> None:
            try:
                prep = workflow.prepare_invoice(clients, po_num, shipment_id)
                text, blocks = format_invoice_preparation(prep)
                say(text=text, blocks=blocks)
            except workflow.PONotFound:
                say(f":question: PO `{po_num}` not found.")
            except workflow.WorkflowError as e:
                say(f":warning: {e}")
            except Exception as e:
                print(
                    f"Error handling /po-invoice {po_num!r} {shipment_id!r}: {e}",
                    file=sys.stderr,
                )
                say(f":rotating_light: Error preparing invoice for PO `{po_num}`: {e}")

        _in_background(f"po-invoice {po_num}", work)

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
        # Replace the original message straight away -- before backgrounding the
        # slow work -- so the button is gone and an impatient double-click can't
        # invoice twice.
        respond(
            replace_original=True,
            text=f":hourglass_flowing_sand: Creating invoices for PO `{po_num}` (requested by {user})...",
        )

        def work() -> None:
            try:
                prep = workflow.prepare_invoice(clients, po_num, shipment_id)

                # Re-derived from scratch rather than trusting the button payload:
                # the PO, the shipment or the price list may have changed since the
                # check, and the click may be hours old.
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

        _in_background(f"po-invoice-confirm {po_num}", work)

    @app.action(slack_notify.PARTIAL_INVOICE_CONFIRM_ACTION)
    def handle_partial_invoice_confirm(ack, body, respond):
        ack()
        try:
            payload = json.loads(body["actions"][0]["value"])
            po_num = payload["po_num"]
            shipment_id = payload["shipment_id"]
        except (KeyError, IndexError, ValueError) as e:
            print(f"Malformed partial-invoice-confirm payload: {e}", file=sys.stderr)
            respond(replace_original=False, text=":rotating_light: Couldn't read that button's data.")
            return

        user = body.get("user", {}).get("username") or body.get("user", {}).get("name", "someone")
        respond(
            replace_original=True,
            text=f":hourglass_flowing_sand: Creating *partial* invoice for PO `{po_num}` (requested by {user})...",
        )

        def work() -> None:
            try:
                prep = workflow.prepare_invoice(clients, po_num, shipment_id)

                # Re-validate that partial invoicing is still allowed
                if not prep.partial_shipment_allowed:
                    text, blocks = format_invoice_preparation(prep)
                    respond(
                        replace_original=True,
                        text=text,
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":no_entry: Nothing invoiced — PO `{po_num}` no longer allows partial invoicing.",
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

                outcome = workflow.execute_partial_invoicing(clients, prep)
                outcome_text = format_invoicing_outcome(outcome)
                blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"PO {po_num} *partially* invoiced as {outcome.invoice_num} (requested by {user})\n{outcome_text}",
                        },
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": slack_notify.SPRING_INVOICE_CONFIRM_ACTION,
                                "text": {"type": "plain_text", "text": "Create Spring invoice"},
                                "value": json.dumps({
                                    "po_num": po_num,
                                    "shipment_id": shipment_id,
                                    "invoice_num": outcome.invoice_num,
                                    "invoice_date": outcome.invoice_date,
                                }),
                                "confirm": {
                                    "title": {"type": "plain_text", "text": "Create Spring invoice?"},
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": (
                                            f"This will fetch shipment data from Spring and create "
                                            f"invoice `{outcome.invoice_num}` with carrier/tracking/weight. "
                                            "The Spring step may transmit an EDI 810 to Target and cannot be undone."
                                        ),
                                    },
                                    "confirm": {"type": "plain_text", "text": "Create"},
                                    "deny": {"type": "plain_text", "text": "Cancel"},
                                },
                            }
                        ],
                    },
                ]
                respond(
                    replace_original=True,
                    text=f"PO {po_num} partially invoiced as {outcome.invoice_num}",
                    blocks=blocks,
                )
            except Exception as e:
                print(f"Error partial-invoicing {po_num!r} {shipment_id!r}: {e}", file=sys.stderr)
                respond(
                    replace_original=True,
                    text=(
                        f":rotating_light: Error partial-invoicing PO `{po_num}`: {e}\n"
                        "Check Odoo and Spring before retrying -- part of it may have gone through."
                    ),
                )

        _in_background(f"po-partial-invoice-confirm {po_num}", work)

    @app.action(slack_notify.SPRING_INVOICE_CONFIRM_ACTION)
    def handle_spring_invoice_confirm(ack, body, respond):
        ack()
        try:
            payload = json.loads(body["actions"][0]["value"])
            po_num = payload["po_num"]
            invoice_num = payload["invoice_num"]
            invoice_date = payload["invoice_date"]
        except (KeyError, IndexError, ValueError) as e:
            print(f"Malformed spring-invoice-confirm payload: {e}", file=sys.stderr)
            respond(replace_original=False, text=":rotating_light: Couldn't read that button's data.")
            return

        user = body.get("user", {}).get("username") or body.get("user", {}).get("name", "someone")
        respond(
            replace_original=True,
            text=f":hourglass_flowing_sand: Creating Spring invoice for PO `{po_num}` (requested by {user})...",
        )

        def work() -> None:
            try:
                # Fetch PO and shipment data
                po = workflow.fetch_po(clients, po_num)

                # Find the Spring shipment for this PO
                shipment_data = workflow.find_spring_shipment_for_po(clients, po_num)
                if not shipment_data:
                    respond(
                        replace_original=True,
                        text=f":warning: No Spring shipment found for PO `{po_num}`. "
                        "Create the invoice manually in Spring.",
                    )
                    return

                # Get shipped quantities from Camelot shipment for partial invoice
                shipment_id = payload.get("shipment_id")
                qty_overrides = None
                if shipment_id:
                    camelot_shipment = clients.camelot.get_shipment_detail(shipment_id)
                    if camelot_shipment:
                        from compare_shipment import evaluate_shipment
                        shipment_result = evaluate_shipment(po, camelot_shipment)
                        qty_overrides = shipment_result.shipped_items

                # Create the Spring invoice with shipment data
                result = workflow.send_spring_invoice(
                    clients, po, invoice_num, invoice_date,
                    qty_overrides=qty_overrides,
                    shipment_data=shipment_data,
                )

                respond(
                    replace_original=True,
                    text=(
                        f":white_check_mark: Spring invoice created for PO `{po_num}` (requested by {user})\n"
                        f":outbox_tray: Invoice id `{result.get('invoice_id')}`, "
                        f"status `{result.get('invoice_status')}`, amount {result.get('invoice_amount')}\n"
                        f":package: Shipment data included: carrier `{shipment_data.get('carrier_name')}`, "
                        f"tracking `{shipment_data.get('tracking_number')}`, "
                        f"weight {shipment_data.get('weight')} {shipment_data.get('weight_uom', 'LB')}"
                    ),
                )
            except Exception as e:
                print(f"Error creating Spring invoice for {po_num!r}: {e}", file=sys.stderr)
                respond(
                    replace_original=True,
                    text=f":rotating_light: Error creating Spring invoice for PO `{po_num}`: {e}",
                )

        _in_background(f"po-spring-invoice-confirm {po_num}", work)

    SocketModeHandler(app, config.slack_app_token).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
