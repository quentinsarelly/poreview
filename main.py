#!/usr/bin/env python3
"""PO pricing review: pull POs from Spring Systems, check line prices against
the price list, and post a pass/fail summary to Slack.

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
import price_list
import state
from camelot_client import CamelotClient
from compare import evaluate_po
from compare_shipment import evaluate_shipment
from config import Config
from slack_notify import format_shipment_summary, format_summary, post_shipment_summary, post_summary
from odoo_client import OdooClient
from spring_client import SpringSystemsClient, build_invoice_request_xml, get_line_items


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
    po_num: str, shipment_id: str, config: Config, args: argparse.Namespace
) -> int:
    if args.from_csv:
        po = next(
            (p for p in csv_po.load_pos_from_csv(args.from_csv) if str(p.get("po_num")) == po_num),
            None,
        )
    else:
        missing = [
            name
            for name, value in [
                ("SPRING_API_BASE_URL", config.spring_base_url),
                ("SPRING_API_USER", config.spring_api_user),
                ("SPRING_API_KEY", config.spring_api_key),
            ]
            if not value
        ]
        if missing:
            print(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in .env, or use --from-csv.",
                file=sys.stderr,
            )
            return 1
        spring_client = SpringSystemsClient(
            base_url=config.spring_base_url,
            api_user=config.spring_api_user,
            api_key=config.spring_api_key,
        )
        po = spring_client.get_po_by_num(po_num)

    if po is None:
        print(f"PO {po_num} not found.", file=sys.stderr)
        return 1

    camelot_missing = [
        name
        for name, value in [
            ("CAMELOT_SOAP_URL", config.camelot_soap_url),
            ("CAMELOT_USERNAME", config.camelot_username),
            ("CAMELOT_PASSWORD", config.camelot_password),
            ("CAMELOT_CLIENT", config.camelot_client_code),
            ("CAMELOT_TRADING_PARTNER", config.camelot_trading_partner),
            ("CAMELOT_SHIPMENT_PROFILE", config.camelot_shipment_profile),
        ]
        if not value
    ]
    if camelot_missing:
        print(
            f"Missing required environment variable(s): {', '.join(camelot_missing)}. "
            "Set them in .env.",
            file=sys.stderr,
        )
        return 1

    camelot_client = CamelotClient(
        soap_url=config.camelot_soap_url,
        username=config.camelot_username,
        password=config.camelot_password,
        client_code=config.camelot_client_code,
        trading_partner=config.camelot_trading_partner,
        shipment_profile=config.camelot_shipment_profile,
    )
    shipment = camelot_client.get_shipment_detail(shipment_id)
    result = evaluate_shipment(po, shipment)

    if args.dry_run:
        print(format_shipment_summary(result))
        return 0

    if not config.slack_webhook_url:
        print("SLACK_WEBHOOK_URL is not set. Set it in .env or use --dry-run.", file=sys.stderr)
        return 1
    post_shipment_summary(result, config.slack_webhook_url)
    print(f"Posted shipment check for PO {po_num} ({result.status.value}) to Slack.")
    return 0


def _missing_env(pairs: list[tuple[str, str | None]]) -> list[str]:
    return [name for name, value in pairs if not value]


def _run_create_invoice(
    po_num: str, invoice_num: str, config: Config, args: argparse.Namespace
) -> int:
    missing = _missing_env(
        [
            ("SPRING_API_BASE_URL", config.spring_base_url),
            ("SPRING_API_USER", config.spring_api_user),
            ("SPRING_API_KEY", config.spring_api_key),
        ]
    )
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}.", file=sys.stderr)
        return 1

    client = SpringSystemsClient(
        base_url=config.spring_base_url,
        api_user=config.spring_api_user,
        api_key=config.spring_api_key,
    )
    po = client.get_po_by_num(po_num)
    if po is None:
        print(f"PO {po_num} not found.", file=sys.stderr)
        return 1

    if args.dry_run:
        # SPRING_VENDOR_ID isn't required just to preview the XML.
        vendor_id = config.spring_vendor_id or "<SPRING_VENDOR_ID not set>"
        invoices_xml = build_invoice_request_xml(po, invoice_num, vendor_id, args.invoice_date)
        pretty = minidom.parseString(
            ElementTree.tostring(invoices_xml, encoding="unicode")
        ).toprettyxml(indent="  ")
        print(pretty)
        return 0

    if not config.spring_vendor_id:
        print("SPRING_VENDOR_ID is not set. Set it in .env or use --dry-run.", file=sys.stderr)
        return 1

    print(
        "WARNING: draft-vs-send behavior for Spring's invoice-incoming/send/ endpoint is "
        "NOT confirmed -- this call may immediately transmit an EDI 810 invoice to the "
        "retailer. Proceeding...",
        file=sys.stderr,
    )
    result = client.create_invoice(po, invoice_num, config.spring_vendor_id, invoice_date=args.invoice_date)
    print(
        f"Created invoice {result.get('invoice_num')} (id={result.get('invoice_id')}, "
        f"status={result.get('invoice_status')}) for PO {po_num}."
    )
    return 0


def _run_list_invoices(date_arg: str, config: Config) -> int:
    missing = _missing_env(
        [
            ("SPRING_API_BASE_URL", config.spring_base_url),
            ("SPRING_API_USER", config.spring_api_user),
            ("SPRING_API_KEY", config.spring_api_key),
        ]
    )
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}.", file=sys.stderr)
        return 1

    date_str = date.today().isoformat() if date_arg == "TODAY" else date_arg
    client = SpringSystemsClient(
        base_url=config.spring_base_url,
        api_user=config.spring_api_user,
        api_key=config.spring_api_key,
    )
    invoices = client.get_invoices_created_since(date_str)
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
    po_num: str, invoice_num: str, config: Config, args: argparse.Namespace
) -> int:
    missing = _missing_env(
        [
            ("SPRING_API_BASE_URL", config.spring_base_url),
            ("SPRING_API_USER", config.spring_api_user),
            ("SPRING_API_KEY", config.spring_api_key),
            ("ODOO_DB_URL", config.odoo_db_url),
            ("ODOO_DB_NAME", config.odoo_db_name),
            ("ODOO_USER", config.odoo_user),
            ("ODOO_API_KEY", config.odoo_api_key),
            ("ODOO_COMPANY_ID", config.odoo_company_id),
            ("ODOO_JOURNAL_ID", config.odoo_journal_id),
            ("ODOO_TARGET_PARTNER_ID", config.odoo_target_partner_id),
        ]
    )
    if missing:
        print(f"Missing required environment variable(s): {', '.join(missing)}.", file=sys.stderr)
        return 1

    spring_client = SpringSystemsClient(
        base_url=config.spring_base_url,
        api_user=config.spring_api_user,
        api_key=config.spring_api_key,
    )
    po = spring_client.get_po_by_num(po_num)
    if po is None:
        print(f"PO {po_num} not found.", file=sys.stderr)
        return 1
    line_items = get_line_items(po)
    if not line_items:
        print(f"PO {po_num} has no line items to invoice.", file=sys.stderr)
        return 1

    odoo = OdooClient(
        url=config.odoo_db_url,
        db=config.odoo_db_name,
        login=config.odoo_user,
        api_key=config.odoo_api_key,
    )

    lines = []
    missing_skus = []
    for item in line_items:
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        product_id = odoo.find_product_id_by_sku(sku)
        if product_id is None:
            missing_skus.append(sku)
            continue
        lines.append(
            {
                "sku": sku,
                "product_id": product_id,
                "quantity": float(item.get("po_item_qty_ordered", 0) or 0),
                "price_unit": float(item.get("po_item_unit_price", 0) or 0),
            }
        )
    if missing_skus:
        print(
            f"SKU(s) not found in Odoo (no product.product with that exact default_code): "
            f"{missing_skus}. Aborting -- fix the product records first.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"Would create DRAFT Odoo invoice for PO {po_num}:")
        print(f"  company_id={config.odoo_company_id} journal_id={config.odoo_journal_id} "
              f"partner_id={config.odoo_target_partner_id}")
        print(f"  ref={po_num!r} payment_reference={invoice_num!r} invoice_date={args.invoice_date!r}")
        for line in lines:
            print(f"  SKU {line['sku']} -> product_id={line['product_id']}, "
                  f"qty={line['quantity']}, price_unit={line['price_unit']}")
        total = sum(line["quantity"] * line["price_unit"] for line in lines)
        print(f"  total={total:.2f}")
        return 0

    move_id = odoo.create_draft_invoice(
        company_id=int(config.odoo_company_id),
        journal_id=int(config.odoo_journal_id),
        partner_id=int(config.odoo_target_partner_id),
        po_num=po_num,
        invoice_num=invoice_num,
        invoice_date=args.invoice_date,
        lines=lines,
    )
    print(f"Created DRAFT Odoo invoice (account.move id={move_id}) for PO {po_num}. "
          f"Not posted -- review and post it in Odoo.")
    return 0


def main() -> int:
    args = parse_args()
    config = Config.load()

    if args.check_shipment:
        po_num, shipment_id = args.check_shipment
        return _run_shipment_check(po_num, shipment_id, config, args)

    if args.create_invoice:
        po_num, invoice_num = args.create_invoice
        return _run_create_invoice(po_num, invoice_num, config, args)

    if args.push_odoo_invoice:
        po_num, invoice_num = args.push_odoo_invoice
        return _run_push_odoo_invoice(po_num, invoice_num, config, args)

    if args.list_invoices:
        return _run_list_invoices(args.list_invoices, config)

    if args.from_csv:
        pos = csv_po.load_pos_from_csv(args.from_csv)
    else:
        missing = [
            name
            for name, value in [
                ("SPRING_API_BASE_URL", config.spring_base_url),
                ("SPRING_API_USER", config.spring_api_user),
                ("SPRING_API_KEY", config.spring_api_key),
                ("SPRING_RETAILER_ID", config.spring_retailer_id),
            ]
            if not value
        ]
        if missing:
            print(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in .env, or use --from-csv to test without the API.",
                file=sys.stderr,
            )
            return 1

        retailer_id = args.retailer_id or config.spring_retailer_id
        client = SpringSystemsClient(
            base_url=config.spring_base_url,
            api_user=config.spring_api_user,
            api_key=config.spring_api_key,
        )
        pos = client.get_pos_for_retailer(retailer_id)

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

    price_map = price_list.load_prices(
        sheet_id=config.google_sheet_id,
        credentials_path=config.google_credentials_path,
        token_path=config.google_token_path,
        worksheet_name=config.price_sheet_worksheet,
        sku_column=config.price_sheet_sku_column,
        price_column=config.price_sheet_price_column,
    )

    for po in pos:
        result = evaluate_po(po, price_map)
        if args.dry_run:
            print(format_summary(result))
            print("-" * 40)
        else:
            post_summary(result, config.slack_webhook_url)
            state.mark_processed(result.po_id)
            print(f"Posted PO {result.po_num} ({result.status.value}) to Slack.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
