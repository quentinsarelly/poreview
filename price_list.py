"""Loads the retailer price list from a Google Sheet, keyed by the retailer's
own SKU (matches po_item_buyer_item_num on the PO).

Uses an OAuth "installed app" flow (same pattern as the Shopify datafeed
automation at code-sarelly/datafeed/sarellydatafeed/main.py) rather than a
service-account key, since service-account key export is blocked by org policy.
On first run this opens a local browser for one-time consent; after that the
cached token in GOOGLE_TOKEN_PATH is reused/refreshed automatically.
"""

import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _authenticate(credentials_path: str, token_path: str) -> Credentials:
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, _SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)
        with open(token_path, "w") as token_file:
            token_file.write(creds.to_json())
    return creds


def load_prices(
    sheet_id: str,
    credentials_path: str,
    token_path: str,
    worksheet_name: str,
    sku_column: str,
    price_column: str,
) -> dict[str, float]:
    creds = _authenticate(credentials_path, token_path)
    service = build("sheets", "v4", credentials=creds)

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=worksheet_name)
        .execute(num_retries=3)
    )
    rows = result.get("values", [])
    if not rows:
        return {}

    header = rows[0]
    try:
        sku_idx = header.index(sku_column)
        price_idx = header.index(price_column)
    except ValueError as e:
        raise RuntimeError(
            f"Expected columns {sku_column!r} and {price_column!r} in the header "
            f"row of worksheet {worksheet_name!r}, found {header!r}"
        ) from e

    price_map: dict[str, float] = {}
    for row in rows[1:]:
        if len(row) <= max(sku_idx, price_idx):
            continue
        sku = row[sku_idx].strip()
        if not sku:
            continue
        price = _parse_price(row[price_idx])
        if price is not None:
            price_map[sku] = price
    return price_map


def _parse_price(raw: str) -> float | None:
    """Strip currency symbols/commas/whitespace (e.g. "$12.00 ") before parsing."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw))
    if not cleaned:
        return None
    return float(cleaned)
