# PO Pricing Review

Pulls incoming EDI purchase orders from Target (via the Spring Systems ERP web
API), checks each line's price against a Google Sheets price list, and posts a
pass/fail summary to Slack for manual confirmation. Also checks a PO's ordered
quantities against what Camelot's WMS actually shipped, ahead of invoicing.
Read-only reporting — it never acknowledges or writes anything back to Spring
Systems, and never submits anything to Camelot.

Two checks, each runnable two ways:
- **Pricing** — PO price vs. the price list.
  - Batch, via CLI (`main.py`) — pulls all new POs for the retailer and posts
    a summary for each.
  - On demand, via Slack (`slack_listener.py`) — `/po-review <po_num>` checks
    one specific PO.
- **Shipment quantities** — PO ordered qty vs. Camelot shipped qty.
  - Via CLI: `main.py --check-shipment <po_num> <shipment_id>`.
  - Via Slack: `/po-ship-check <po_num> <shipment_id>`.

  Both need the **Camelot shipment ID** (e.g. `S0461276`) as well as the PO #.
  There's currently no working way to look up a Target/EDI shipment by PO #
  alone — Camelot's date-range search (`GetOrderStatusDateRange`) only ever
  returns DTC/TikTok shipments, confirmed by testing against a real Target PO
  whose shipment fell squarely inside the searched window and still didn't
  show up. Until that's resolved (or Camelot confirms the right call), find
  the shipment ID manually in Camelot's UI.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values below
```

### Environment variables (`.env`)

| Variable | Required for | Notes |
|---|---|---|
| `SPRING_API_BASE_URL` | live API pulls | Use `portalapp.springsystems.com` (production). The `staging-*` host has a broken TLS chain — don't point at it. |
| `SPRING_API_USER` / `SPRING_API_KEY` | live API pulls | Production Spring Systems API credentials. |
| `SPRING_RETAILER_ID` | live API pulls | The retailer's Spring `tp_id`, **not** your own vendor/company name. Target's is `135`. Spring's sandbox/demo data lives under `699` — don't confuse the two. |
| `GOOGLE_SHEET_ID` | always | The price list spreadsheet. |
| `GOOGLE_CREDENTIALS_PATH` | always | OAuth "installed app" client secret (not a service-account key — service-account key export is blocked by org policy). |
| `GOOGLE_TOKEN_PATH` | always | Where the cached OAuth token is stored after first login. |
| `PRICE_SHEET_WORKSHEET` / `PRICE_SHEET_SKU_COLUMN` / `PRICE_SHEET_PRICE_COLUMN` | always | Which tab/columns hold the SKU and expected price. |
| `SLACK_WEBHOOK_URL` | `main.py` (non `--dry-run`) | Incoming webhook for posting batch summaries. |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `slack_listener.py` | Bot (`xoxb-`) and app-level (`xapp-`) tokens for the `/po-review`/`/po-ship-check` Socket Mode listener. See below. |
| `CAMELOT_SOAP_URL` / `CAMELOT_USERNAME` / `CAMELOT_PASSWORD` | shipment-quantity checks | Camelot 3PL (Excalibur) SOAP credentials. |
| `CAMELOT_CLIENT` / `CAMELOT_TRADING_PARTNER` | shipment-quantity checks | Excalibur client/trading-partner codes for the account. |
| `CAMELOT_SHIPMENT_PROFILE` | shipment-quantity checks | The Excalibur **interface profile** bound to the Shipment Export XMLPort (`SAR_SHP_E` on this account) — Camelot's `pInterfaceProfile` determines which data shape a call returns, not the SOAP action name, so the inventory-only profile used by other Sarelly repos (`SAR_ITEM_E`) will not work here. |

The Spring/price-list SKU join key is `product.product_vendor_item_num` (our
own SKU) — **not** `po_item_buyer_item_num` (the retailer's internal item
number). Camelot's `ItemNumber` on a shipped line matches
`product_vendor_item_num` exactly (confirmed against a real Target PO), so
the same SKU is used to join both checks.

## Running the batch CLI

```bash
python main.py --dry-run              # print summaries instead of posting to Slack
python main.py                        # post new POs' summaries to Slack
python main.py --inspect-status       # print raw po_acknowledge_status values (setup helper)
python main.py --force                # re-evaluate POs even if already marked processed
python main.py --retailer-id <id>     # override SPRING_RETAILER_ID from .env
python main.py --from-csv po.csv --dry-run   # test against a local CSV export, no API needed
```

"New" is tracked in `processed_pos.json`; a PO is only skipped there once a
non-dry-run run has posted it.

Some real POs come back from Spring with no line items yet synced
(`<po_items><po_item/></po_items>`) — these are reported as `NO_LINE_ITEMS`
rather than a false `ALL_MATCH`, and should be re-run later.

### Running a shipment quantity check

```bash
python main.py --check-shipment <po_num> <shipment_id> --dry-run
```

e.g.:

```bash
python main.py --check-shipment 10001964460-3841 S0461276 --dry-run
```

This takes **two arguments, not just the PO #**: the PO # from Spring, and
Camelot's own shipment ID (e.g. `S0461276`), which you have to look up
yourself in Camelot's UI — see the note above on why there's no automatic
PO#→shipment lookup yet. `--dry-run` prints the result to the terminal; drop
it to post to Slack via `SLACK_WEBHOOK_URL` instead. This is a one-off check
and does not touch `processed_pos.json`.

A PO with no matching Camelot shipment (not yet shipped, or a wrong/mistyped
shipment ID) is reported as `NOT_YET_SHIPPED` rather than a false `ALL_MATCH`.

## Setting up the Slack slash commands

The listener uses Slack **Socket Mode**, so it needs no public URL or ngrok
tunnel — it opens an outbound websocket connection to Slack.

1. Go to **https://api.slack.com/apps** and open your app (or create a new
   one) — this is where every step below happens.
2. **Socket Mode** (left sidebar) → enable it. This generates an app-level
   token (`xapp-...`) with the `connections:write` scope — put it in `.env`
   as `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → add bot token scopes `commands` and
   `chat:write` → install (or reinstall) the app to your workspace. Copy the
   **Bot User OAuth Token** (`xoxb-...`) into `.env` as `SLACK_BOT_TOKEN`.
4. **Slash Commands** → create `/po-review` and `/po-ship-check`. The Request
   URL field can be left blank/placeholder for both — Socket Mode doesn't
   call it.
5. Invite the bot to the channel where you want to use these commands.
6. Run it:
   ```bash
   python slack_listener.py
   ```
   You should see Bolt log that it's connected. In Slack, run
   `/po-review <po_num>` for a known PO and confirm the summary posts back
   to the channel, and `/po-ship-check <po_num> <shipment_id>` (Camelot
   shipment ID found manually in Camelot's UI) for the quantity check.

The listener does **not** touch `processed_pos.json` — both commands are ad
hoc, one-off checks, independent of the batch CLI's dedup state.

**Already have `/po-review` set up and just need to add `/po-ship-check`?**
You only need step 4 again — no new app, no new tokens:
1. Go to **https://api.slack.com/apps**, open the existing app.
2. **Slash Commands** → **Create New Command** → name it `/po-ship-check`
   (Request URL can be left blank/placeholder, same as `/po-review`).
3. Add `CAMELOT_*` credentials to `.env` if you haven't (see the env var
   table above) — the listener won't start without them.
4. Restart `slack_listener.py` (or restart the systemd service, if it's
   already installed as one) to pick up the new command.

### Running it as a background service (systemd)

A unit template is at `deploy/poreview-slack-listener.service`. To install:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/poreview-slack-listener.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now poreview-slack-listener.service
loginctl enable-linger "$USER"   # keeps it running without an active login session
```

Check on it with `systemctl --user status poreview-slack-listener.service`
and `journalctl --user -u poreview-slack-listener.service -f`.

**Caveat:** this only keeps the slash commands alive while this machine is
powered on, awake, and WSL is running — fine for a single-person MVP, but not
a reliable deployment for a team to depend on. If this proves useful, move
`slack_listener.py` (unchanged) to a small always-on VPS instead of running
it here.

## Project layout

| File | Purpose |
|---|---|
| `main.py` | Batch CLI entry point; also the `--check-shipment` one-off shipment check. |
| `slack_listener.py` | `/po-review` and `/po-ship-check` Socket Mode listener. |
| `spring_client.py` | Spring Systems API client (XML responses, header-based pagination). |
| `camelot_client.py` | Camelot 3PL (Excalibur SOAP) client for shipment quantity lookups. |
| `csv_po.py` | Loads Spring's flattened CSV export format, for testing without API access. |
| `price_list.py` | Loads/parses the Google Sheets price list (OAuth). |
| `compare.py` | Core price-comparison logic (`evaluate_po`, `POStatus`/`LineStatus`). |
| `compare_shipment.py` | Shipment quantity-comparison logic (`evaluate_shipment`, `ShipmentStatus`/`ShipmentLineStatus`). |
| `slack_notify.py` | Formats `POResult`/`ShipmentResult` into Slack message text and posts via webhook. |
| `state.py` | Tracks which PO IDs have already been posted (`processed_pos.json`), used only by the batch CLI. |
| `config.py` | Loads and validates all `.env` settings. |
| `deploy/` | systemd unit template for running the Slack listener as a service. |

## Testing without live API access

```bash
python main.py --from-csv po_test.csv --dry-run
```

Loads a Spring Systems CSV export and runs it through the exact same
comparison/Slack logic as the live API path.
