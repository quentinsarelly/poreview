"""Loads the retailer price list from a Google Sheet, keyed by our own SKU
(product_vendor_item_num on the PO -- NOT po_item_buyer_item_num, which is
the retailer's internal item number; see compare.py).

Uses an OAuth "installed app" flow (same pattern as the Shopify datafeed
automation at code-sarelly/datafeed/sarellydatafeed/main.py) rather than a
service-account key, since service-account key export is blocked by org policy.

Two ways to supply that OAuth grant:

1. GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET -- the
   credentials are rebuilt in memory on each process start and nothing is ever
   written to disk. This is the deployed path: a refresh token doesn't rotate
   when it's used (only the short-lived access token does), so there is no
   durable file to persist and no volume needed.
2. GOOGLE_TOKEN_PATH -- a cached token file, written on first consent. This is
   the local-development path.

Interactive consent only runs when stdin is a TTY. Headless (Railway, systemd,
cron) it raises instead, because run_local_server() would otherwise block
forever waiting for a browser visit that can never happen.
"""

import os
import re
import sys

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"

_REAUTH_HELP = (
    "Re-authorize by running any command that loads the price list from a "
    "terminal on a machine with a browser (e.g. `python main.py --from-csv "
    "po_test.csv --dry-run`), then copy the refresh_token out of the resulting "
    "token file into GOOGLE_REFRESH_TOKEN.\n"
    "If this keeps happening roughly weekly, the OAuth consent screen is still "
    "in 'Testing' status -- Google expires those refresh tokens after 7 days. "
    "Publish the consent screen to stop it."
)


def _credentials_from_env() -> Credentials | None:
    """Credentials rebuilt from env vars, or None if they aren't all set."""
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        return None
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=_TOKEN_URI,
        scopes=_SCOPES,
    )


def _authenticate(
    credentials_path: str, token_path: str, allow_interactive: bool | None = None
) -> Credentials:
    if allow_interactive is None:
        allow_interactive = sys.stdin.isatty()

    creds = _credentials_from_env()
    from_env = creds is not None
    if creds is None and os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)

    # Env-built credentials always start invalid (no access token yet), so
    # refresh whenever we hold a refresh token rather than checking .expired --
    # which is False when there's no expiry to compare against.
    if creds is not None and not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                source = (
                    "GOOGLE_REFRESH_TOKEN" if from_env else f"the token file at {token_path}"
                )
                raise RuntimeError(
                    f"Google rejected the refresh token from {source}: {e}\n{_REAUTH_HELP}"
                ) from e
        else:
            creds = None

    if creds is not None and creds.valid:
        if not from_env:
            # Only persist when we're the file-based path; the env path is
            # deliberately stateless so an ephemeral filesystem is fine.
            with open(token_path, "w") as token_file:
                token_file.write(creds.to_json())
        return creds

    if not allow_interactive:
        raise RuntimeError(
            "No usable Google credentials and no terminal to authorize from.\n"
            "Set GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET "
            f"(preferred when deployed), or provide a valid token file at {token_path}.\n"
            f"{_REAUTH_HELP}"
        )

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
