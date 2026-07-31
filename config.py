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

    google_sheet_id: str
    google_credentials_path: str
    google_token_path: str
    price_sheet_worksheet: str
    price_sheet_sku_column: str
    price_sheet_price_column: str

    # Optional: only needed when actually posting to Slack (not for --dry-run).
    slack_webhook_url: str | None

    # Optional: only needed when running slack_listener.py (the /po-review slash command).
    slack_bot_token: str | None
    slack_app_token: str | None

    @classmethod
    def load(cls) -> "Config":
        return cls(
            spring_base_url=os.getenv("SPRING_API_BASE_URL"),
            spring_api_user=os.getenv("SPRING_API_USER"),
            spring_api_key=os.getenv("SPRING_API_KEY"),
            spring_retailer_id=os.getenv("SPRING_RETAILER_ID"),
            google_sheet_id=_require("GOOGLE_SHEET_ID"),
            google_credentials_path=_require("GOOGLE_CREDENTIALS_PATH"),
            google_token_path=os.getenv("GOOGLE_TOKEN_PATH", "./google-token.json"),
            price_sheet_worksheet=os.getenv("PRICE_SHEET_WORKSHEET", "Sheet1"),
            price_sheet_sku_column=os.getenv("PRICE_SHEET_SKU_COLUMN", "sku"),
            price_sheet_price_column=os.getenv(
                "PRICE_SHEET_PRICE_COLUMN", "expected_unit_price"
            ),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN") or None,
            slack_app_token=os.getenv("SLACK_APP_TOKEN") or None,
        )


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value
