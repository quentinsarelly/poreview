#!/usr/bin/env python3
"""PO pricing review: pull POs from Spring Systems, check line prices against
the price list, and post a pass/fail summary to Slack.

This is the CLI adapter over workflow.py -- it parses flags, renders results to
the terminal or Slack, and maps failures to exit codes. The actual Spring /
Camelot / Odoo logic lives in workflow.py, shared with slack_listener.py.

Usage:
    python main.py --dry-run              # print summaries instead of posting to Slack
    python main.py                        # post new POs' summaries to Slack
    python main.py --inspect-status       # print raw po_acknowledge_status values (setup helper)
    python main.py --force                # re-evaluate POs even if already marked processed
    python main.py --retailer-id <id>     # override SPRING_RETAILER_ID from .env
    python main.py --from-csv po.csv --dry-run   # test against a local CSV export, no API needed

    # One-off shipment quantity check (PO vs what Camelot actually shipped).
    # There's no automatic PO#->shipment lookup for Target orders yet, so the
    # Camelot shipment ID (e.g. S0461276) must be found manually in Camelot's UI.
    python main.py --check-shipment 10001964460-3841 S0461276 --dry-run

    # One-off invoice creation for a PO (invoices it as ordered -- run only after
    # the PO has already passed --dry-run pricing review). --dry-run prints the
    # XML that would be sent instead of sending it -- see the WARNING in
    # SpringSystemsClient.create_invoice before ever running this for real.
    python main.py --create-invoice 10001964460-3841 TAR26081342 --invoice-date 2026-08-14 --dry-run

    # List invoices created on/after a date (default: today) -- replaces the
    # manual "export today's invoices" step in Spring's UI.
    python main.py --list-invoices
    python main.py --list-invoices 2026-08-01

    # Push a matching draft invoice into Odoo (account.move, state=draft --
    # never posted). Uses the PO's line items directly, independent of whether
    # --create-invoice has been run in Spring. --dry-run prints what would be
    # created without touching Odoo.
    python main.py --push-odoo-invoice 10001964460-3841 TAR26081342 --invoice-date 2026-08-14 --dry-run
"""

import argparse
import sys
from datetime import date
from xml.dom import minidom
from xml.etree import ElementTree

import csv_po
import state
import workflow
from config import Config
from slack_notify import format_shipment_summary, format_summary, post_shipment_summary, post_summary

_CSV_HINT = "Set them in .env, or use --from-csv."
_ENV_HINT = "Set them in .env."
_BATCH_HINT = "Set them in .env, or use --from-csv to test without the API."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print comparison summaries to the terminal instead of posting to Slack.",
    )
    parser.add_argument(
        "--inspect-status",
        action="store_true",
        help="Print each fetched PO's po_acknowledge_status and exit "
        "(use this once to confirm what 'new' looks like before relying on filters).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate POs even if already recorded in processed_pos.json.",
    )
    parser.add_argument(
        "--retailer-id",
        help="Override SPRING_RETAILER_ID from the environment for this run.",
    )
    parser.add_argument(
        "--from-csv",
        metavar="PATH",
        help="Load PO(s) from a local Spring Systems CSV export instead of calling the API "
        "(for testing before API credentials are available).",
    )
    parser.add_argument(
        "--check-shipment",
        nargs=2,
        metavar=("PO_NUM", "SHIPMENT_ID"),
        help="One-off shipment quantity check for a PO # against a Camelot shipment ID, "
        "e.g. --check-shipment 10001964460-3841 S0461276. The Camelot shipment ID must be "
        "found manually in Camelot's UI -- there's no working PO#->shipment lookup for "
        "Target orders yet. Prints with --dry-run, otherwise posts to Slack.",
    )
    parser.add_argument(
        "--create-invoice",
        nargs=2,
        metavar=("PO_NUM", "INVOICE_NUM"),
        help="Create/send an invoice for a PO (invoices it as ordered), e.g. "
        "--create-invoice 10001964460-3841 TAR26081342. Requires SPRING_VENDOR_ID in "
        "the environment. With --dry-run, prints the XML that would be sent instead of "
        "sending it -- see the WARNING in SpringSystemsClient.create_invoice before ever "
        "running this without --dry-run.",
    )
    parser.add_argument(
        "--invoice-date",
        metavar="YYYY-MM-DD",
        help="Invoice date to set when using --create-invoice (optional).",
    )
    parser.add_argument(
        "--push-odoo-invoice",
        nargs=2,
        metavar=("PO_NUM", "INVOICE_NUM"),
        help="Create a matching DRAFT invoice (account.move, never posted) in Odoo from "
        "a PO's line items, e.g. --push-odoo-invoice 10001964460-3841 TAR26081342. "
        "Independent of Spring's --create-invoice. With --dry-run, prints what would be "
        "created without touching Odoo.",
    )
    parser.add_argument(
        "--list-invoices",
        nargs="?",
        const="TODAY",
        metavar="YYYY-MM-DD",
        help="List invoices created on/after a date (default: today) -- replaces the "
        "manual 'export today's invoices' step in Spring's UI.",
    )
    return parser.parse_args()


def _run_shipment_check(
    po_num: str, shipment_id: str, clients: workflow.Clients, args: argparse.Namespace
) -> int:
    if not args.from_csv:
        workflow.require_spring_env(clients.config, _CSV_HINT)
    po = workflow.fetch_po(clients, po_num, from_csv=args.from_csv)
    workflow.require_camelot_env(clients.config, _ENV_HINT)
    result = workflow.check_shipment_quantities(clients, po, shipment_id)

    if args.dry_run:
        print(format_shipment_summary(result))
        return 0

    if not clients.config.slack_webhook_url:
        print("SLACK_WEBHOOK_URL is not set. Set it in .env or use --dry-run.", file=sys.stderr)
        return 1
    post_shipment_summary(result, clients.config.slack_webhook_url)
    print(f"Posted shipment check for PO {po_num} ({result.status.value}) to Slack.")
    return 0


def _run_create_invoice(
    po_num: str, invoice_num: str, clients: workflow.Clients, args: argparse.Namespace
) -> int:
    po = workflow.fetch_po(clients, po_num)

    if args.dry_run:
        invoices_xml = workflow.build_spring_invoice_xml(
            clients.config, po, invoice_num, args.invoice_date
        )
        print(
            minidom.parseString(
                ElementTree.tostring(invoices_xml, encoding="unicode")
            ).toprettyxml(indent="  ")
        )
        return 0

    print(
        "WARNING: draft-vs-send behavior for Spring's invoice-incoming/send/ endpoint is "
        "NOT confirmed -- this call may immediately transmit an EDI 810 invoice to the "
        "retailer. Proceeding...",
        file=sys.stderr,
    )
    result = workflow.send_spring_invoice(clients, po, invoice_num, args.invoice_date)
    print(
        f"Created invoice {result.get('invoice_num')} (id={result.get('invoice_id')}, "
        f"status={result.get('invoice_status')}) for PO {po_num}."
    )
    return 0


def _run_list_invoices(date_arg: str, clients: workflow.Clients) -> int:
    date_str = date.today().isoformat() if date_arg == "TODAY" else date_arg
    invoices = workflow.list_invoices_since(clients, date_str)
    if not invoices:
        print(f"No invoices created on/after {date_str}.")
        return 0
    for inv in invoices:
        print(
            f"invoice_id={inv.get('invoice_id')} invoice_num={inv.get('invoice_num')} "
            f"amount={inv.get('invoice_amount')} status={inv.get('invoice_status')} "
            f"created={inv.get('invoice_created')} "
            f"retailer={inv.get('retailer', {}).get('retailer_name')}"
        )
    return 0


def _run_push_odoo_invoice(
    po_num: str, invoice_num: str, clients: workflow.Clients, args: argparse.Namespace
) -> int:
    workflow.require_odoo_push_env(clients.config)
    po = workflow.fetch_po(clients, po_num)
    plan = workflow.plan_odoo_invoice(clients, po, invoice_num, args.invoice_date)

    if args.dry_run:
        print(f"Would create DRAFT Odoo invoice for PO {po_num}:")
        print(
            f"  company_id={plan.company_id} journal_id={plan.journal_id} "
            f"partner_id={plan.partner_id}"
        )
        print(
            f"  ref={plan.po_num!r} payment_reference={plan.invoice_num!r} "
            f"invoice_date={plan.invoice_date!r}"
        )
        for line in plan.lines:
            print(
                f"  SKU {line.sku} -> product_id={line.product_id}, "
                f"qty={line.quantity}, price_unit={line.price_unit}"
            )
        print(f"  total={plan.total:.2f}")
        return 0

    move_id = workflow.create_odoo_invoice(clients, plan)
    print(
        f"Created DRAFT Odoo invoice (account.move id={move_id}) for PO {po_num}. "
        f"Not posted -- review and post it in Odoo."
    )
    return 0


def _run_batch_review(clients: workflow.Clients, args: argparse.Namespace) -> int:
    config = clients.config
    if args.from_csv:
        pos = csv_po.load_pos_from_csv(args.from_csv)
    else:
        workflow.require_batch_env(config, _BATCH_HINT)
        retailer_id = args.retailer_id or config.spring_retailer_id
        pos = workflow.fetch_retailer_pos(clients, retailer_id)

    if args.inspect_status:
        for po in pos:
            print(
                f"po_id={po.get('po_id')} po_num={po.get('po_num')} "
                f"po_acknowledge_status={po.get('po_acknowledge_status')!r}"
            )
        return 0

    if not args.force:
        processed = state.load_processed_ids()
        pos = [po for po in pos if str(po.get("po_id")) not in processed]

    if not pos:
        print("No new POs to review.")
        return 0

    if not args.dry_run and not config.slack_webhook_url:
        print("SLACK_WEBHOOK_URL is not set. Set it in .env or use --dry-run.", file=sys.stderr)
        return 1

    price_map = workflow.load_price_map(config)

    for po in pos:
        result = workflow.review_pricing(po, price_map)
        if args.dry_run:
            print(format_summary(result))
            print("-" * 40)
        else:
            post_summary(result, config.slack_webhook_url)
            state.mark_processed(result.po_id)
            print(f"Posted PO {result.po_num} ({result.status.value}) to Slack.")

    return 0


def _dispatch(args: argparse.Namespace, clients: workflow.Clients) -> int:
    if args.check_shipment:
        po_num, shipment_id = args.check_shipment
        return _run_shipment_check(po_num, shipment_id, clients, args)

    if args.create_invoice:
        po_num, invoice_num = args.create_invoice
        return _run_create_invoice(po_num, invoice_num, clients, args)

    if args.push_odoo_invoice:
        po_num, invoice_num = args.push_odoo_invoice
        return _run_push_odoo_invoice(po_num, invoice_num, clients, args)

    if args.list_invoices:
        return _run_list_invoices(args.list_invoices, clients)

    return _run_batch_review(clients, args)


def main() -> int:
    args = parse_args()
    config = Config.load()
    clients = workflow.Clients(config)
    try:
        return _dispatch(args, clients)
    except workflow.WorkflowError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
