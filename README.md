# PO Pricing Review

Pulls incoming EDI purchase orders from Target (via the Spring Systems ERP web
API), checks each line's price against a Google Sheets price list, and posts a
pass/fail summary to Slack for manual confirmation. Also checks a PO's ordered
quantities against what Camelot's WMS actually shipped, ahead of invoicing,
and can create the resulting invoice in Spring (`--create-invoice`, see
below and **Open questions**). Everything except invoice creation is
read-only reporting; it never submits anything to Camelot.

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

**Both checks plus invoicing, in one command:** `/po-invoice <po_num>
<shipment_id>` runs the pricing check and the shipment check, derives the
invoice number and date, checks Spring and Odoo for an existing invoice, and
— only if everything passes — offers a button that creates the invoices. See
"Invoicing a PO from Slack" below.

## Open questions

- **Spring invoice creation: draft vs. send is unconfirmed.** `--create-invoice`
  (see "Creating an invoice for a PO" below) POSTs to Spring's
  `invoice-incoming/send/` endpoint. Nothing in Spring's docs says whether
  that creates a draft or immediately transmits an EDI 810 invoice to the
  retailer — it's the same style of endpoint used to create/acknowledge POs.
  One hint (not proof): every real, already-sent Target invoice pulled via
  `--list-invoices` shows `invoice_status=1`, while Spring's own generic docs
  example returned `invoice_status=5` right after creating one via this same
  API. **Resolve this — by asking Spring Systems support directly, or by
  running one deliberate real test — before ever using `--create-invoice`
  without `--dry-run`.** See the WARNING docstring on
  `SpringSystemsClient.create_invoice`.

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
| `SPRING_VENDOR_ID` | `--create-invoice` | Our own vendor `tp_id` in Spring (Sarelly's, not the retailer's) — `33145`, confirmed via a real PO's `<vendor_id>` field. |
| `SPRING_INVOICE_ENABLED` | `/po-invoice`'s Spring leg | `1`/`true`/`yes`/`on` to enable; **off by default**. While off, `/po-invoice` runs every check and creates the Odoo draft but sends nothing to Spring or Target. See "Enabling the Spring leg". |
| `GOOGLE_SHEET_ID` | always | The price list spreadsheet. |
| `GOOGLE_CREDENTIALS_PATH` | always | OAuth "installed app" client secret (not a service-account key — service-account key export is blocked by org policy). |
| `GOOGLE_TOKEN_PATH` | always | Where the cached OAuth token is stored after first login. |
| `PRICE_SHEET_WORKSHEET` / `PRICE_SHEET_SKU_COLUMN` / `PRICE_SHEET_PRICE_COLUMN` | always | Which tab/columns hold the SKU and expected price. |
| `SLACK_WEBHOOK_URL` | `main.py` (non `--dry-run`) | Incoming webhook for posting batch summaries. |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `slack_listener.py` | Bot (`xoxb-`) and app-level (`xapp-`) tokens for the Socket Mode listener (`/po-invoice`, `/po-review`, `/po-ship-check`). See below. |
| `CAMELOT_SOAP_URL` / `CAMELOT_USERNAME` / `CAMELOT_PASSWORD` | shipment-quantity checks | Camelot 3PL (Excalibur) SOAP credentials. |
| `CAMELOT_CLIENT` / `CAMELOT_TRADING_PARTNER` | shipment-quantity checks | Excalibur client/trading-partner codes for the account. |
| `CAMELOT_SHIPMENT_PROFILE` | shipment-quantity checks | The Excalibur **interface profile** bound to the Shipment Export XMLPort (`SAR_SHP_E` on this account) — Camelot's `pInterfaceProfile` determines which data shape a call returns, not the SOAP action name, so the inventory-only profile used by other Sarelly repos (`SAR_ITEM_E`) will not work here. |
| `ODOO_DB_URL` / `ODOO_DB_NAME` / `ODOO_USER` / `ODOO_API_KEY` | `--push-odoo-invoice`, `/po-invoice` | `ODOO_API_KEY` must be a dedicated API key (avatar → My Profile → Account Security → New API Key, set to **Persistent**), not your login password — Odoo Online blocks password auth on the external API. |
| `ODOO_COMPANY_ID` / `ODOO_JOURNAL_ID` / `ODOO_TARGET_PARTNER_ID` | `--push-odoo-invoice`, `/po-invoice` | Fixed IDs for Target invoices in this Odoo instance — `2` (SARELLY USA LLC), `33` (Sales/INV journal), `7642` (Target Stores, Inc.) — confirmed by inspecting a real existing Target invoice. |

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

### Creating an invoice for a PO

```bash
python main.py --create-invoice <po_num> <invoice_num> [--invoice-date YYYY-MM-DD] --dry-run
```

e.g.:

```bash
python main.py --create-invoice 10001964460-3841 TAR26081342 --invoice-date 2026-08-14 --dry-run
```

Invoices the PO **as ordered** (same qty/price as the PO — run this only
after a `--dry-run` pricing review has already passed). `--dry-run` prints
the XML that would be POST-ed to `invoice-incoming/send/` instead of sending
it; dropping `--dry-run` actually creates the invoice in Spring.

**⚠️ Unconfirmed: draft vs. send.** Spring's docs don't document any way to
create a draft invoice distinct from one that's immediately transmitted (EDI
810) to the retailer via this endpoint — it's the same style of endpoint used
to create/acknowledge POs. Before ever running this without `--dry-run`,
either confirm the actual behavior with Spring Systems support, or go in
understanding a real send may happen immediately. The field mapping itself
(PO → line items → invoice XML) has been validated: a `--dry-run` against a
real, already-invoiced PO reproduced the exact same `invoice_amount` Spring
already has on file for it.

### Listing invoices

```bash
python main.py --list-invoices [YYYY-MM-DD]   # default: today
```

Replaces the manual "export today's invoices" step in Spring's UI — read-only,
safe to run anytime.

### Pushing an invoice to Odoo

```bash
python main.py --push-odoo-invoice <po_num> <invoice_num> [--invoice-date YYYY-MM-DD] --dry-run
```

e.g.:

```bash
python main.py --push-odoo-invoice 10001964460-3841 TAR26081342 --invoice-date 2026-08-14 --dry-run
```

**Confirmed working end-to-end (2026-08-14)** — creates a **draft**
`account.move` in Odoo directly via the external API (XML-RPC), matched line
by line against the PO's SKUs via exact `product.product.default_code`
lookup. Nothing is posted or transmitted; it's a plain draft you review and
post yourself in Odoo, same as any manually entered invoice. This replaces
the CSV export/reformat/upload step entirely — the CSV workflow is no longer
needed going forward. Independent of Spring's `--create-invoice` (uses the
PO's data directly, not Spring's invoice record), so it can be run whether or
not you've also invoiced in Spring.

Currently Target-only: `ODOO_TARGET_PARTNER_ID`/`ODOO_COMPANY_ID`/
`ODOO_JOURNAL_ID` are single fixed IDs (see env var table), matching the
single-retailer assumption `SPRING_RETAILER_ID` already makes elsewhere in
this tool. Extend to a real retailer→partner mapping if a second retailer is
added.

If a SKU on the PO doesn't have a matching Odoo product (`default_code`
exact match), the whole push aborts before creating anything — fix the
product record in Odoo first rather than push a partial invoice.

## Invoicing a PO from Slack

```
/po-invoice 10001993952-3840 S0461276
```

Runs the whole flow and stops at the first thing that isn't right:

1. **Pricing check** — must be `ALL_MATCH`.
2. **Shipment quantity check** — must be `ALL_MATCH`.
3. **Ship date** — taken from Camelot's `ShipDate`, and cross-checked against
   Spring's `po_last_asn_date`. If the two disagree the run stops; see below.
4. **Invoice number** — derived as `TAR` + `YYMMDD` (ship date) + the PO's
   trailing 2 digits (its Target DC code). PO `10001993952-3840` shipped
   `2026-08-18` → `TAR26081840`.
5. **Duplicate check** — Spring and Odoo are both searched for that invoice
   number, since two POs to the same DC shipping the same day would otherwise
   derive the same one.

If anything blocks, Slack shows every blocker at once and no button. If
everything passes, it posts the derived invoice number, date and total with a
**Create invoices** button (behind a confirmation dialog).

Clicking it **re-runs all of the above from scratch** rather than trusting the
button's payload — the PO, shipment or price list may have changed since the
check, and the click may be hours old. If anything has drifted, nothing is
invoiced and Slack says what changed. The original message is replaced the
moment the button is clicked, so a double-click can't invoice twice.

Then it creates the **Odoo draft invoice first** (reversible), and only then
the Spring invoice (which may transmit an EDI 810 to Target, and is not).
If Odoo fails, Spring is never called.

### The ship-date cross-check

Camelot's ship date is authoritative. Spring has no ASN export endpoint
(`asn-outgoing`, `shipment-outgoing` etc. all 404), so its only ASN trace is
`po_last_asn_date` — the timestamp the ASN was *transmitted*, which is not the
same as the date the goods shipped. An ASN sent after midnight, or stamped in
a different timezone, would be off by a day.

That matters because this one date sets **both** the invoice date and the
invoice number, and the invoice number is what Target reconciles against. So a
disagreement between the two sources is a hard stop, not a warning.

### Enabling the Spring leg

`SPRING_INVOICE_ENABLED` is off by default, and while it's off `/po-invoice`
runs every check and creates the Odoo draft but does **not** send anything to
Spring or Target — it reports the Spring step as skipped.

Turn it on only once Spring Systems has:
1. granted this API user permission for `invoice-incoming/send` (it currently
   returns `405 "You do not have permission to use this resource"`), and
2. confirmed whether that call creates a draft or immediately transmits an
   EDI 810 to Target.

### Previewing from the CLI

```bash
python main.py --prepare-invoice 10001993952-3840 S0461276
```

Runs steps 1–5 and prints the result, including any blockers. Read-only: it
never creates an invoice. Exits `0` when ready to invoice, `1` when blocked.

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
4. **Slash Commands** → create `/po-invoice`, `/po-review` and
   `/po-ship-check`. The Request URL field can be left blank/placeholder for
   all of them — Socket Mode doesn't call it.
   **Interactivity & Shortcuts** → toggle **Interactivity** on (the Request
   URL there can also be left blank under Socket Mode). Without this the
   `/po-invoice` confirm button does nothing when clicked.
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
| `main.py` | CLI adapter over `workflow.py`: batch review, one-off checks, `--prepare-invoice`. |
| `slack_listener.py` | Slack adapter over `workflow.py`: `/po-invoice`, `/po-review`, `/po-ship-check`. |
| `workflow.py` | Shared business logic both entry points call. Returns values or raises `WorkflowError`; never prints or exits. |
| `invoicing.py` | Pure derivation rules for the invoice number and invoice date, plus the Camelot/Spring ship-date cross-check. |
| `spring_client.py` | Spring Systems API client (XML responses, header-based pagination). |
| `camelot_client.py` | Camelot 3PL (Excalibur SOAP) client for shipment quantity lookups. |
| `csv_po.py` | Loads Spring's flattened CSV export format, for testing without API access. |
| `price_list.py` | Loads/parses the Google Sheets price list (OAuth). |
| `compare.py` | Core price-comparison logic (`evaluate_po`, `POStatus`/`LineStatus`). |
| `compare_shipment.py` | Shipment quantity-comparison logic (`evaluate_shipment`, `ShipmentStatus`/`ShipmentLineStatus`). |
| `slack_notify.py` | Formats `POResult`/`ShipmentResult`/`InvoicePreparation` into Slack text and Block Kit, and posts via webhook. |
| `state.py` | Tracks which PO IDs have already been posted (`processed_pos.json`), used only by the batch CLI. |
| `config.py` | Loads and validates all `.env` settings. |
| `deploy/` | systemd unit template for running the Slack listener as a service. |

## Testing without live API access

```bash
python main.py --from-csv po_test.csv --dry-run
```

Loads a Spring Systems CSV export and runs it through the exact same
comparison/Slack logic as the live API path.
