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
"""

import argparse
import sys

import csv_po
import price_list
import state
from compare import evaluate_po
from config import Config
from slack_notify import format_summary, post_summary
from spring_client import SpringSystemsClient


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config.load()

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
