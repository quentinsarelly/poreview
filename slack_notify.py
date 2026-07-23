import time

import requests

from compare import LineStatus, POResult, POStatus

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2

_STATUS_EMOJI = {
    LineStatus.OK: ":white_check_mark:",
    LineStatus.PRICE_MISMATCH: ":warning:",
    LineStatus.SKU_NOT_FOUND: ":question:",
}


def format_summary(result: POResult) -> str:
    header_emoji = ":white_check_mark:" if result.status == POStatus.ALL_MATCH else ":rotating_light:"
    lines = [
        f"{header_emoji} *PO {result.po_num}* ({result.retailer_name}) — {result.status.value}",
    ]
    for line in result.lines:
        emoji = _STATUS_EMOJI[line.status]
        if line.status == LineStatus.SKU_NOT_FOUND:
            lines.append(
                f"{emoji} SKU `{line.sku or '(blank)'}` (retailer item# `{line.retailer_item_num}`) "
                f"— not found in price list (PO price: {line.price_ordered:.2f}, qty {line.qty_ordered})"
            )
        elif line.status == LineStatus.PRICE_MISMATCH:
            lines.append(
                f"{emoji} SKU `{line.sku}` — PO price {line.price_ordered:.2f} "
                f"vs expected {line.price_expected:.2f} (delta {line.delta:+.2f}), "
                f"qty {line.qty_ordered}"
            )
        else:
            lines.append(
                f"{emoji} SKU `{line.sku}` — {line.price_ordered:.2f}, qty {line.qty_ordered}"
            )
    return "\n".join(lines)


def post_summary(result: POResult, webhook_url: str) -> None:
    text = format_summary(result)
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = requests.post(webhook_url, json={"text": text}, timeout=15)
            response.raise_for_status()
            return
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise last_error
