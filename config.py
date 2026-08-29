import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Optional: only needed when fetching from the Spring Systems API (not for --from-csv).
    spring_base_url: str | None
    spring_api_user: str | None
    spring_api_key: str | None
    spring_retailer_id: str | None
    # Optional: only needed for --create-invoice (our own vendor tp_id in Spring).
    spring_vendor_id: str | None
    # Gates the Spring leg of /po-invoice. Off until Spring grants the API user
    # permission for invoice-incoming/send AND confirms whether that call creates
    # a draft or immediately transmits an EDI 810 to Target.
    spring_invoice_enabled: bool

    google_sheet_id: str
    google_credentials_path: str
    google_token_path: str
    price_sheet_worksheet: str
    price_sheet_sku_column: str
    price_sheet_price_column: str
    # How long a loaded price list is reused before re-reading the sheet. Only
    # meaningful in the long-lived listener; a CLI run loads it at most once.
    price_cache_seconds: int

    # Optional: only needed when actually posting to Slack (not for --dry-run).
    slack_webhook_url: str | None

    # Optional: only needed when running slack_listener.py (the /po-review slash command).
    slack_bot_token: str | None
    slack_app_token: str | None

    # Optional: only needed for shipment-quantity checks (Camelot WMS), not for pricing checks.
    camelot_soap_url: str | None
    camelot_username: str | None
    camelot_password: str | None
    camelot_client_code: str | None
    camelot_trading_partner: str | None
    camelot_shipment_profile: str | None

    # Optional: only needed for --push-odoo-invoice.
    odoo_db_url: str | None
    odoo_db_name: str | None
    odoo_user: str | None
    odoo_api_key: str | None
    odoo_company_id: str | None
    odoo_journal_id: str | None
    odoo_target_partner_id: str | None

    @classmethod
    def load(cls) -> "Config":
        return cls(
            spring_base_url=os.getenv("SPRING_API_BASE_URL"),
            spring_api_user=os.getenv("SPRING_API_USER"),
            spring_api_key=os.getenv("SPRING_API_KEY"),
            spring_retailer_id=os.getenv("SPRING_RETAILER_ID"),
            spring_vendor_id=os.getenv("SPRING_VENDOR_ID"),
            spring_invoice_enabled=_flag("SPRING_INVOICE_ENABLED"),
            google_sheet_id=_require("GOOGLE_SHEET_ID"),
            google_credentials_path=os.getenv(
                "GOOGLE_CREDENTIALS_PATH", "./google-credentials.json"
            ),
            google_token_path=os.getenv("GOOGLE_TOKEN_PATH", "./google-token.json"),
            price_sheet_worksheet=os.getenv("PRICE_SHEET_WORKSHEET", "Sheet1"),
            price_sheet_sku_column=os.getenv("PRICE_SHEET_SKU_COLUMN", "sku"),
            price_sheet_price_column=os.getenv(
                "PRICE_SHEET_PRICE_COLUMN", "expected_unit_price"
            ),
            price_cache_seconds=_int("PRICE_CACHE_SECONDS", 300),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN") or None,
            slack_app_token=os.getenv("SLACK_APP_TOKEN") or None,
            camelot_soap_url=os.getenv("CAMELOT_SOAP_URL") or None,
            camelot_username=os.getenv("CAMELOT_USERNAME") or None,
            camelot_password=os.getenv("CAMELOT_PASSWORD") or None,
            camelot_client_code=os.getenv("CAMELOT_CLIENT") or None,
            camelot_trading_partner=os.getenv("CAMELOT_TRADING_PARTNER") or None,
            camelot_shipment_profile=os.getenv("CAMELOT_SHIPMENT_PROFILE") or None,
            odoo_db_url=os.getenv("ODOO_DB_URL") or None,
            odoo_db_name=os.getenv("ODOO_DB_NAME") or None,
            odoo_user=os.getenv("ODOO_USER") or None,
            odoo_api_key=os.getenv("ODOO_API_KEY") or None,
            odoo_company_id=os.getenv("ODOO_COMPANY_ID") or None,
            odoo_journal_id=os.getenv("ODOO_JOURNAL_ID") or None,
            odoo_target_partner_id=os.getenv("ODOO_TARGET_PARTNER_ID") or None,
        )


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}.") from e


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value
