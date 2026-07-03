"""
tally_pipeline_item_invoice.py
===============================
Full pipeline — ITEM INVOICE version:
  1. Fetch invoice from MongoDB by invoice number
  2. Convert to Tally-compatible structure (with line items as STOCK ITEMS)
  3. Resolve target Tally company from TALLY_COMPANY env var or FY-based auto-name
  4. Ensure vendor ledger, purchase ledger, and each STOCK ITEM exist — create if missing
  5. Push the purchase voucher to Tally as an ITEM INVOICE (OBJVIEW="Invoice Voucher View")
  6. Log every step, warning, and error to console + tally_pipeline_item_invoice.log

Key differences from the accounting-voucher version:
  - VCHTYPE uses OBJVIEW="Invoice Voucher View" + ISINVOICE=Yes (not "Accounting Voucher View")
  - Each line item becomes a STOCK ITEM (ALLINVENTORYENTRIES.LIST), not its own ledger
  - All stock items post through a single PURCHASE_LEDGER (e.g. "Purchase A/c"), not
    one ledger per item description
  - Party ledger entry carries ISPARTYLEDGER=Yes + BILLALLOCATIONS.LIST (bill-wise ref)
  - Stock items are auto-created (under a configurable Stock Group) the same way
    ledgers are auto-created

Prerequisites:
  - TallyPrime must be running with the target company already open in the UI.
  - Set TALLY_COMPANY in .env to the exact company name shown in TallyPrime's
    title bar.  If not set, the pipeline auto-derives the name from the invoice
    date as {TALLY_COMPANY_PREFIX}_FY{yy}{yy+1} (e.g. IDP_FY2526).

Usage:
  Set INVOICE_NUMBER below, then run:
      pip install pymongo python-dotenv requests
      python tally_pipeline_item_invoice.py
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

# ─────────────────────────────────────────────
#  LOAD .env FIRST so os.environ.get() picks up
#  values from the file at module-import time.
# ─────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION  – change these as needed
# ─────────────────────────────────────────────
INVOICE_NUMBER = "KLKA2526-12078"  # <── set your invoice number here

TALLY_URL          = os.environ.get("TALLY_URL", "http://localhost:9000")
TALLY_TIMEOUT      = 10                        # seconds
TALLY_LONG_TIMEOUT = 120                       # company list / heavy exports

# TALLY_COMPANY: exact name of the company already open in TallyPrime (required).
# Visible in TallyPrime's title bar.  If not set, name is auto-derived from the
# invoice's Indian financial year as {prefix}_FY{yy}{yy+1} (e.g. IDP_FY2526).
TALLY_COMPANY_ENV    = os.environ.get("TALLY_COMPANY", "").strip()
TALLY_COMPANY_PREFIX = os.environ.get("TALLY_COMPANY_PREFIX", "IDP").strip() or "IDP"

# INVOICE_DATE_OVERRIDE: set this (YYYY-MM-DD) to force a specific invoice date
# when extraction produced a wrong date.
INVOICE_DATE_OVERRIDE = os.environ.get("INVOICE_DATE_OVERRIDE", "").strip()

# Dates this many days in the future are flagged as implausible for an invoice.
INVOICE_DATE_MAX_FUTURE_DAYS = int(os.environ.get("INVOICE_DATE_MAX_FUTURE_DAYS", "30"))

# MongoDB (can also be set via .env)
MONGO_URI        = os.environ.get("MONGO_URI",                 "mongodb://localhost:27017")
MONGO_DB         = os.environ.get("MONGO_DB",                  "IDP")
MONGO_COLLECTION = os.environ.get("MONGO_INVOICES_COLLECTION", "invoices")

# Ledger → Tally group mapping
VENDOR_LEDGER_GROUP = os.environ.get("VENDOR_LEDGER_GROUP", "Sundry Creditors")

# Single accounting ledger that ALL stock items post their purchase value through.
# (Item invoice mode does not need a separate ledger per line item.)
PURCHASE_LEDGER       = os.environ.get("PURCHASE_LEDGER", "Purchase A/c")
PURCHASE_LEDGER_GROUP = os.environ.get("PURCHASE_LEDGER_GROUP", "Purchase Accounts")

# Stock item defaults, used only when auto-creating a missing stock item.
STOCK_GROUP     = os.environ.get("STOCK_GROUP", "Primary")
STOCK_UOM       = os.environ.get("STOCK_UOM", "Nos")  # Unit of measure — must exist in Tally

# ─────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────
LOG_FILE = Path("tally_pipeline_item_invoice.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tally_pipeline_item_invoice")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _normalize_date(value: Any) -> str:
    """Return date as YYYYMMDD for Tally."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    formats = [
        "%d-%b-%Y", "%d %b %Y",
        "%d-%b-%y", "%d %b %y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            result = parsed.strftime("%Y%m%d")
            log.debug("Date '%s' matched format '%s' → %s", text, fmt, result)

            delta_days = (parsed - datetime.today()).days
            if delta_days > INVOICE_DATE_MAX_FUTURE_DAYS:
                log.warning(
                    "Invoice date '%s' parsed as %s which is %d days in the future. "
                    "Set INVOICE_DATE_OVERRIDE=YYYY-MM-DD in .env to force the correct date.",
                    text, result, delta_days,
                )
            return result
        except ValueError:
            continue

    log.warning("Could not parse date '%s' with any known format – leaving as-is", text)
    return text


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _xml_esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _sanitize_voucher_ref(value: str) -> str:
    """Strip '/', '\\', '-' from voucher refs so Tally never misreads them as dates."""
    s = str(value or "").strip()
    return re.sub(r"[/\\\-]+", "", s)


def _map_line_items(g: dict) -> list[dict]:
    """
    Map raw line items to stock-item-oriented dicts.
    Each item needs: item_name, quantity, rate (unit price), amount, unit.
    """
    src = g.get("line_items") or []
    out = []
    if isinstance(src, list):
        for li in src:
            if not isinstance(li, dict):
                continue
            qty        = _to_float(li.get("quantity") or li.get("qty")) or 0.0
            unit_price = _to_float(li.get("unit_price") or li.get("price_per_unit") or li.get("rate"))
            line_total = _to_float(li.get("line_total") or li.get("amount"))
            item_name  = _safe_str(li.get("description") or li.get("service") or li.get("item_name"))
            unit       = _safe_str(li.get("unit") or li.get("uom")) or STOCK_UOM

            if line_total is None and (qty and unit_price is not None):
                line_total = round(qty * unit_price, 2)

            if line_total is None:
                log.warning(
                    "Line item '%s' has no line_total and missing qty/unit_price – skipping.",
                    item_name or "<unknown>",
                )
                continue

            if not unit_price and qty:
                unit_price = round(line_total / qty, 2)
            elif not unit_price:
                unit_price = line_total
                qty = qty or 1.0

            out.append({
                "item_name":  item_name or "Unspecified Item",
                "hsn":        _safe_str(li.get("hsn") or li.get("hsn_number") or li.get("HSN")),
                "quantity":   qty or 1.0,
                "unit_price": unit_price,
                "unit":       unit,
                "amount":     abs(line_total),
            })
    return out


def _convert_doc(doc: dict) -> dict:
    """Convert raw MongoDB document to intermediate invoice dict."""
    raw_g = (doc.get("gemini") or {}).get("json") or {}

    if isinstance(raw_g, str):
        try:
            raw_g = json.loads(raw_g)
            log.debug("gemini.json was a JSON string – parsed successfully.")
        except json.JSONDecodeError as exc:
            log.warning("gemini.json is a string but not valid JSON (%s) – treating as empty.", exc)
            raw_g = {}

    g: dict = raw_g if isinstance(raw_g, dict) else {}
    if not g:
        log.warning(
            "gemini.json is missing or not a dict (type=%s) – invoice fields will be empty.",
            type(raw_g).__name__,
        )

    vendor_obj = g.get("vendor") if isinstance(g.get("vendor"), dict) else {}

    result = {
        "invoice_number": _safe_str(g.get("invoice_number") or g.get("invoice")),
        "invoice_date":   _normalize_date(g.get("invoice_date") or g.get("date")),
        "vendor_name":    _safe_str(vendor_obj.get("name") or g.get("seller")),
        "line_items":     _map_line_items(g),
        "total_amount":   _to_float(g.get("total_amount") or g.get("grand_total") or g.get("amount")),
        "currency":       _safe_str(g.get("currency")),
        "narration":      _safe_str(g.get("narration") or g.get("description")),
    }

    if INVOICE_DATE_OVERRIDE:
        override_normalized = _normalize_date(INVOICE_DATE_OVERRIDE)
        if override_normalized:
            log.info(
                "INVOICE_DATE_OVERRIDE applied: replacing '%s' with '%s'",
                result["invoice_date"], override_normalized,
            )
            result["invoice_date"] = override_normalized
        else:
            log.warning(
                "INVOICE_DATE_OVERRIDE='%s' could not be parsed – ignoring override.",
                INVOICE_DATE_OVERRIDE,
            )

    return result


# ─────────────────────────────────────────────
#  XML BUILDERS — MASTERS
# ─────────────────────────────────────────────
def _xml_create_ledger(name: str, parent: str, tally_company: str) -> str:
    ne, pe, ce = _xml_esc(name), _xml_esc(parent), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER Action="Create">
            <NAME>{ne}</NAME>
            <PARENT>{pe}</PARENT>
          </LEDGER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def _xml_check_ledger(name: str, tally_company: str) -> str:
    ne, ce = _xml_esc(name), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Accounts</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
          <LEDGERNAME>{ne}</LEDGERNAME>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""


def _xml_create_stock_item(name: str, unit: str, group: str, tally_company: str) -> str:
    ne, ue, ge, ce = _xml_esc(name), _xml_esc(unit), _xml_esc(group), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <STOCKITEM Action="Create">
            <NAME>{ne}</NAME>
            <PARENT>{ge}</PARENT>
            <BASEUNITS>{ue}</BASEUNITS>
          </STOCKITEM>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def _xml_check_stock_item(name: str, tally_company: str) -> str:
    ne, ce = _xml_esc(name), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Stock Items</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
          <STOCKITEMNAME>{ne}</STOCKITEMNAME>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""


def _xml_check_unit(name: str, tally_company: str) -> str:
    ne, ce = _xml_esc(name), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Units</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""


def _xml_create_unit(name: str, tally_company: str) -> str:
    ne, ce = _xml_esc(name), _xml_esc(tally_company)
    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <UNIT Action="Create">
            <NAME>{ne}</NAME>
            <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
          </UNIT>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


# ─────────────────────────────────────────────
#  XML BUILDER — ITEM INVOICE VOUCHER
# ─────────────────────────────────────────────
def _build_voucher_xml(inv: dict, tally_company: str) -> str:
    """
    Build a Purchase voucher XML in ITEM INVOICE mode.

    Tally sign convention for Purchase Item Invoices:
      - Vendor (Sundry Creditor)  = CREDIT side  → positive amount, ISDEEMEDPOSITIVE=No
      - Each stock item entry     = DEBIT side   → negative amount, ISDEEMEDPOSITIVE=Yes
        (mirrored by its ACCOUNTINGALLOCATIONS.LIST entry against PURCHASE_LEDGER)
      - Debit total (sum of |item amounts|) MUST equal Credit total (vendor amount)

    If line items don't sum to total_amount, the last line item is auto-adjusted
    so Tally never receives an unbalanced voucher (which causes silent
    EXCEPTIONS=1 / CREATED=0 rejections).
    """
    date = inv["invoice_date"] or datetime.today().strftime("%Y%m%d")
    vch_number_raw = _safe_str(inv["invoice_number"])
    vch_number_safe = _xml_esc(_sanitize_voucher_ref(vch_number_raw))
    narration_raw = _safe_str(inv.get("narration"))
    if vch_number_raw and _sanitize_voucher_ref(vch_number_raw) != vch_number_raw:
        audit = f"SourceInvoiceRef={vch_number_raw}"
        narration_raw = f"{narration_raw} | {audit}" if narration_raw else audit
    vendor = _xml_esc(inv["vendor_name"])
    ce = _xml_esc(tally_company)
    purchase_ledger_esc = _xml_esc(PURCHASE_LEDGER)
    total = inv["total_amount"] or 0.0
    narration = _xml_esc(narration_raw)

    items = inv["line_items"]
    if not items:
        log.warning("No line items found – creating a single catch-all stock item for total amount.")
        items = [{
            "item_name": "Unspecified Item", "hsn": "", "quantity": 1.0,
            "unit_price": abs(total), "unit": STOCK_UOM, "amount": abs(total),
        }]
        inv["line_items"] = items

    # ── Balance check & auto-correction ──────────────────────────────────
    line_amounts = [it["amount"] for it in items]
    lines_sum = round(sum(line_amounts), 2)
    total_abs = round(abs(total), 2)
    if total_abs and lines_sum != total_abs:
        diff = round(total_abs - lines_sum, 2)
        log.warning(
            "Voucher imbalance detected: line items sum to %.2f but total_amount is %.2f "
            "(diff=%.2f). Auto-adjusting last line item to balance.",
            lines_sum, total_abs, diff,
        )
        items[-1]["amount"] = round(items[-1]["amount"] + diff, 2)
        if items[-1]["amount"] < 0:
            log.error(
                "Auto-adjustment produced a negative line amount (%.2f). "
                "Check your invoice data – voucher may still be rejected.",
                items[-1]["amount"],
            )
    final_total = round(sum(it["amount"] for it in items), 2) or total_abs

    # ── Party ledger entry: CREDIT side, with bill allocation ─────────────
    party_entry = f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{vendor}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
              <AMOUNT>{final_total:.2f}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <NAME>{vch_number_safe}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{final_total:.2f}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>"""

    # ── Stock item entries: DEBIT side, each with its own accounting allocation ──
    inventory_entries = []
    for it in items:
        item_name = _xml_esc(it["item_name"])
        unit = _xml_esc(it["unit"] or STOCK_UOM)
        qty = it["quantity"] or 1.0
        rate = it["unit_price"] if it["unit_price"] is not None else it["amount"]
        amt = it["amount"]

        inventory_entries.append(f"""            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{item_name}</STOCKITEMNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <RATE>{rate:.2f}/{unit}</RATE>
              <ACTUALQTY>{qty:g} {unit}</ACTUALQTY>
              <BILLEDQTY>{qty:g} {unit}</BILLEDQTY>
              <AMOUNT>-{amt:.2f}</AMOUNT>
              <ACCOUNTINGALLOCATIONS.LIST>
                <LEDGERNAME>{purchase_ledger_esc}</LEDGERNAME>
                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                <AMOUNT>-{amt:.2f}</AMOUNT>
              </ACCOUNTINGALLOCATIONS.LIST>
            </ALLINVENTORYENTRIES.LIST>""")

    inventory_xml = "\n".join(inventory_entries)

    log.debug(
        "Voucher balance: credit(party)=%.2f  debit(items)=%.2f",
        final_total, sum(it["amount"] for it in items),
    )

    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{ce}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJVIEW="Invoice Voucher View">
            <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
            <DATE>{date}</DATE>
            <EFFECTIVEDATE>{date}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
            <ISINVOICE>Yes</ISINVOICE>
            <PARTYNAME>{vendor}</PARTYNAME>
            <PARTYLEDGERNAME>{vendor}</PARTYLEDGERNAME>
            <BASICBUYERNAME>{vendor}</BASICBUYERNAME>
            <REFERENCE>{vch_number_safe}</REFERENCE>
            <NARRATION>{narration}</NARRATION>
            <ISOPTIONAL>No</ISOPTIONAL>
{party_entry}
{inventory_xml}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


# ─────────────────────────────────────────────
#  TALLY COMMUNICATION
# ─────────────────────────────────────────────
def _post_tally(xml: str, label: str, timeout_s: Optional[int] = None) -> Optional[str]:
    log.debug("Posting to Tally [%s] ...", label)
    t = timeout_s if timeout_s is not None else TALLY_TIMEOUT
    try:
        resp = requests.post(
            TALLY_URL,
            data=xml.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=t,
        )
        resp.raise_for_status()
        log.debug("Tally response [%s]: %s", label, resp.text[:300])
        return resp.text
    except requests.exceptions.ConnectionError:
        log.error("Cannot connect to Tally at %s – is TallyPrime open?", TALLY_URL)
        return None
    except requests.exceptions.Timeout:
        log.error("Tally request timed out after %ds [%s]", t, label)
        return None
    except requests.exceptions.HTTPError as exc:
        log.error("HTTP error from Tally [%s]: %s", label, exc)
        return None
    except Exception as exc:
        log.error("Unexpected error posting to Tally [%s]: %s", label, exc)
        return None


def _is_tally_date_error(resp: str) -> bool:
    upper = resp.upper()
    return "OUT OF RANGE" in upper or "VOUCHER DATE IS MISSING" in upper


def _fallback_voucher_dates(primary: str) -> list[str]:
    fallbacks: list[str] = []
    if len(primary) == 8 and primary.isdigit():
        fallbacks.append(primary[:6] + "01")
    override = _normalize_date(INVOICE_DATE_OVERRIDE)
    if override:
        fallbacks.append(override)
    fallbacks.append(datetime.today().strftime("%Y%m%d"))
    fallbacks.append("20260401")

    seen = {primary}
    ordered: list[str] = []
    for d in fallbacks:
        if d and d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def _parse_tally_response(resp: str) -> dict:
    def _int(tag: str) -> int:
        m = re.search(rf"<{tag}>(\d+)</{tag}>", resp, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    return {
        "created":       _int("CREATED"),
        "altered":       _int("ALTERED"),
        "errors":        _int("ERRORS"),
        "exceptions":    _int("EXCEPTIONS"),
        "has_lineerror": "LINEERROR" in resp.upper(),
    }


def _resolve_tally_company(inv: dict) -> str:
    if TALLY_COMPANY_ENV:
        log.info("Using TALLY_COMPANY from env: '%s'", TALLY_COMPANY_ENV)
        return TALLY_COMPANY_ENV

    date_str = inv.get("invoice_date") or ""
    if len(date_str) == 8 and date_str.isdigit():
        dt = datetime.strptime(date_str, "%Y%m%d")
    else:
        dt = datetime.today()
        log.warning("Invoice date missing or unparsable – using today (%s) for FY name.", dt.strftime("%Y%m%d"))

    fy_start = dt.year if dt.month >= 4 else dt.year - 1
    y0, y1   = fy_start % 100, (fy_start + 1) % 100
    name     = f"{TALLY_COMPANY_PREFIX}_FY{y0:02d}{y1:02d}"
    log.info("Auto-derived Tally company name: '%s' (set TALLY_COMPANY in .env to override)", name)
    return name


def _ledger_exists_in_tally(name: str, tally_company: str) -> bool:
    xml  = _xml_check_ledger(name, tally_company)
    resp = _post_tally(xml, f"check_ledger:{name}")
    if resp is None:
        return False
    escaped_name = re.escape(name)
    pattern = rf"<NAME[^>]*>\s*{escaped_name}\s*</NAME>"
    exists = bool(re.search(pattern, resp, re.IGNORECASE))
    log.debug("Ledger '%s' exists in Tally: %s", name, exists)
    return exists


def _ensure_ledger(name: str, group: str, tally_company: str) -> bool:
    log.info("Checking ledger: '%s'", name)
    if _ledger_exists_in_tally(name, tally_company):
        log.info("  ✓ Ledger '%s' already exists – skipping creation", name)
        return True

    log.info("  ✗ Ledger '%s' not found – creating under '%s'", name, group)
    xml  = _xml_create_ledger(name, group, tally_company)
    resp = _post_tally(xml, f"create_ledger:{name}")
    if resp is None:
        log.error("  Failed to create ledger '%s' – no response from Tally", name)
        return False

    parsed = _parse_tally_response(resp)
    if parsed["has_lineerror"]:
        log.error("  Tally reported a LINEERROR creating ledger '%s': %s", name, resp[:400])
        return False
    if parsed["errors"] > 0:
        log.error("  Tally reported %d error(s) creating ledger '%s': %s", parsed["errors"], name, resp[:400])
        return False
    if parsed["created"] > 0 or parsed["altered"] > 0:
        log.info("  ✓ Ledger '%s' created/updated successfully (created=%d, altered=%d)",
                 name, parsed["created"], parsed["altered"])
        return True

    log.warning(
        "  Ledger '%s': unexpected Tally response (created=0, errors=0). Treating as success. Response: %s",
        name, resp[:400],
    )
    return True


# Tally ships with these as built-in simple units — they always exist, and
# attempting to "create" or export-check them causes either a false-negative
# (report name varies by Tally version, e.g. "List of Units" not recognized)
# or a GUI confirmation popup that hangs the HTTP request until it times out.
# Skip both check and create for anything in this set (case-insensitive).
DEFAULT_TALLY_UNITS = {
    "nos", "no", "kg", "kgs", "gm", "gms", "ltr", "ltrs", "ml",
    "mtr", "mtrs", "cm", "mm", "pcs", "pc", "box", "set", "pair",
    "doz", "dz", "u", "unt", "cs", "bag", "bottle", "bundle",
}


def _unit_exists_in_tally(name: str, tally_company: str) -> bool:
    xml  = _xml_check_unit(name, tally_company)
    resp = _post_tally(xml, f"check_unit:{name}")
    if resp is None:
        return False
    escaped_name = re.escape(name)
    pattern = rf"<NAME[^>]*>\s*{escaped_name}\s*</NAME>"
    exists = bool(re.search(pattern, resp, re.IGNORECASE))
    log.debug("Unit '%s' exists in Tally: %s", name, exists)
    return exists


def _ensure_unit(name: str, tally_company: str) -> bool:
    if name.strip().lower() in DEFAULT_TALLY_UNITS:
        log.info("Unit '%s' is a built-in Tally unit – skipping check/create", name)
        return True

    log.info("Checking unit of measure: '%s'", name)
    if _unit_exists_in_tally(name, tally_company):
        log.info("  ✓ Unit '%s' already exists – skipping creation", name)
        return True

    log.info("  ✗ Unit '%s' not found – creating", name)
    log.warning(
        "  '%s' is not in the built-in unit list — if this hangs/times out, Tally is likely "
        "showing a GUI confirmation popup. Check the Tally window and dismiss it manually.",
        name,
    )
    xml  = _xml_create_unit(name, tally_company)
    resp = _post_tally(xml, f"create_unit:{name}")
    if resp is None:
        log.error("  Failed to create unit '%s' – no response from Tally", name)
        return False

    parsed = _parse_tally_response(resp)
    if parsed["has_lineerror"]:
        log.error("  Tally reported a LINEERROR creating unit '%s': %s", name, resp[:400])
        return False
    if parsed["errors"] > 0:
        log.error("  Tally reported %d error(s) creating unit '%s': %s", parsed["errors"], name, resp[:400])
        return False

    log.info("  ✓ Unit '%s' created/updated (created=%d, altered=%d)",
             name, parsed["created"], parsed["altered"])
    return True


def _stock_item_exists_in_tally(name: str, tally_company: str) -> bool:
    xml  = _xml_check_stock_item(name, tally_company)
    resp = _post_tally(xml, f"check_stock_item:{name}")
    if resp is None:
        return False
    escaped_name = re.escape(name)
    pattern = rf"<NAME[^>]*>\s*{escaped_name}\s*</NAME>"
    exists = bool(re.search(pattern, resp, re.IGNORECASE))
    log.debug("Stock item '%s' exists in Tally: %s", name, exists)
    return exists


def _ensure_stock_item(name: str, unit: str, tally_company: str) -> bool:
    log.info("Checking stock item: '%s'", name)

    # Make sure the unit exists first — Tally rejects a stock item referencing
    # an unknown unit with a silent exception, same failure mode as ledgers.
    if not _ensure_unit(unit, tally_company):
        log.error("  Cannot ensure stock item '%s' – required unit '%s' could not be created", name, unit)
        return False

    if _stock_item_exists_in_tally(name, tally_company):
        log.info("  ✓ Stock item '%s' already exists – skipping creation", name)
        return True

    log.info("  ✗ Stock item '%s' not found – creating under group '%s' (unit=%s)", name, STOCK_GROUP, unit)
    xml  = _xml_create_stock_item(name, unit, STOCK_GROUP, tally_company)
    resp = _post_tally(xml, f"create_stock_item:{name}")
    if resp is None:
        log.error("  Failed to create stock item '%s' – no response from Tally", name)
        return False

    parsed = _parse_tally_response(resp)
    if parsed["has_lineerror"]:
        log.error("  Tally reported a LINEERROR creating stock item '%s': %s", name, resp[:400])
        return False
    if parsed["errors"] > 0:
        log.error("  Tally reported %d error(s) creating stock item '%s': %s", parsed["errors"], name, resp[:400])
        return False
    if parsed["created"] > 0 or parsed["altered"] > 0:
        log.info("  ✓ Stock item '%s' created/updated successfully (created=%d, altered=%d)",
                 name, parsed["created"], parsed["altered"])
        return True

    log.warning(
        "  Stock item '%s': unexpected Tally response (created=0, errors=0). Treating as success. Response: %s",
        name, resp[:400],
    )
    return True


def _evaluate_voucher_response(resp: str, out_path: Path) -> bool:
    parsed = _parse_tally_response(resp)
    log.debug("Voucher response parsed: %s", parsed)

    if parsed["has_lineerror"]:
        log.error("Tally rejected the voucher with LINEERROR:\n%s", resp)
        return False
    if parsed["errors"] > 0:
        log.error("Tally reported %d error(s) in voucher response:\n%s", parsed["errors"], resp)
        return False
    if parsed["exceptions"] > 0 and parsed["created"] == 0:
        log.error(
            "Tally returned EXCEPTIONS=%d with CREATED=0 – voucher was NOT saved. Check the saved XML at: %s",
            parsed["exceptions"], out_path,
        )
        return False
    if parsed["exceptions"] > 0 and parsed["created"] > 0:
        log.warning(
            "Voucher created but Tally reported %d exception(s) – review in TallyPrime.",
            parsed["exceptions"],
        )
    if parsed["created"] > 0:
        log.info("Voucher pushed successfully (CREATED=%d)", parsed["created"])
        return True

    log.error("Voucher push returned CREATED=0 with no explicit error. Full response:\n%s", resp)
    return False


def _push_voucher(inv: dict, tally_company: str) -> bool:
    log.info("Building item-invoice voucher XML for invoice '%s'", inv["invoice_number"])

    safe_filename = inv["invoice_number"].replace("/", "-").replace("\\", "-")
    out_path = Path("invoices_data") / "tally_xml" / f"{safe_filename}.xml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    primary_date = inv["invoice_date"] or datetime.today().strftime("%Y%m%d")
    dates_to_try = [primary_date] + _fallback_voucher_dates(primary_date)
    last_resp: Optional[str] = None

    for attempt_idx, voucher_date in enumerate(dates_to_try):
        inv_attempt = dict(inv)
        inv_attempt["invoice_date"] = voucher_date
        narration = _safe_str(inv.get("narration"))
        if voucher_date != primary_date:
            audit = f"SourceInvoiceDate={primary_date}"
            inv_attempt["narration"] = f"{narration} | {audit}" if narration else audit
            log.warning(
                "Retrying voucher push with alternate Tally date %s (invoice date was %s).",
                voucher_date, primary_date,
            )

        xml = _build_voucher_xml(inv_attempt, tally_company)
        out_path.write_text(xml, encoding="utf-8")
        if attempt_idx == 0:
            log.info("XML saved to: %s", out_path)

        log.info("Pushing voucher to Tally (date=%s) ...", voucher_date)
        resp = _post_tally(xml, f"push_voucher:{voucher_date}")
        if resp is None:
            return False
        last_resp = resp

        if _evaluate_voucher_response(resp, out_path):
            if voucher_date != primary_date:
                log.info(
                    "Posted with alternate Tally date %s; original invoice date %s kept in narration.",
                    voucher_date, primary_date,
                )
            return True

        if not _is_tally_date_error(resp) or attempt_idx == len(dates_to_try) - 1:
            return False

    return False if last_resp is None else _evaluate_voucher_response(last_resp, out_path)


# ─────────────────────────────────────────────
#  MAIN PIPELINE
# ─────────────────────────────────────────────
def main() -> None:
    load_dotenv()

    invoice_number = INVOICE_NUMBER.strip()
    if not invoice_number:
        log.error("INVOICE_NUMBER is not set. Please set it at the top of this script.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("Starting Tally ITEM INVOICE pipeline for invoice: %s", invoice_number)
    log.info("=" * 60)

    # ── STEP 1: Fetch from MongoDB ──────────────────────────────
    log.info("[STEP 1] Connecting to MongoDB at %s ...", MONGO_URI)
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()
        coll = client[MONGO_DB][MONGO_COLLECTION]
        log.info("  Connected to DB '%s', collection '%s'", MONGO_DB, MONGO_COLLECTION)
    except Exception as exc:
        log.error("  MongoDB connection failed: %s", exc)
        sys.exit(1)

    log.info("[STEP 1] Searching for invoice '%s' ...", invoice_number)
    doc = coll.find_one(
        {"$or": [
            {"gemini.json.invoice_number": invoice_number},
            {"gemini.json.invoice":        invoice_number},
        ]},
        sort=[("_id", -1)],
    )

    if not doc:
        log.error("  No data found for invoice number '%s'. Exiting.", invoice_number)
        sys.exit(1)

    log.info("  ✓ Invoice found in MongoDB (document _id: %s)", doc.get("_id"))

    # ── STEP 2: Convert document ────────────────────────────────
    log.info("[STEP 2] Converting document to invoice structure ...")
    try:
        inv = _convert_doc(doc)
        log.info("  ✓ Converted successfully")
        log.info("    Vendor      : %s", inv["vendor_name"])
        log.info("    Date        : %s", inv["invoice_date"])
        log.info("    Total       : %s", inv["total_amount"])
        log.info("    Line items  : %d", len(inv["line_items"]))
        for it in inv["line_items"]:
            log.info("      - %s | qty=%s %s | rate=%s | amount=%s",
                     it["item_name"], it["quantity"], it["unit"], it["unit_price"], it["amount"])

        if not inv["vendor_name"]:
            log.warning("  Vendor name is empty – check MongoDB document structure")
        if not inv["invoice_date"]:
            log.warning("  Invoice date missing – will use today's date")
        if not inv["total_amount"]:
            log.warning("  Total amount is 0 or missing")
        if not inv["line_items"]:
            log.warning("  No line items found – a catch-all stock item will be used")

    except Exception as exc:
        log.error("  Document conversion failed: %s", exc)
        sys.exit(1)

    safe_inv_filename = invoice_number.replace("/", "-").replace("\\", "-")
    json_path = Path("invoices_data") / "tally_json" / f"{safe_inv_filename}_converted.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(inv, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("  Converted JSON saved to: %s", json_path)

    # ── STEP 3: Company selection ───────────────────────────────
    log.info("[STEP 3] Resolving target Tally company name ...")
    tally_company = _resolve_tally_company(inv)
    log.info("  Using Tally company: '%s'", tally_company)

    # ── STEP 4: Ensure vendor ledger, purchase ledger, and stock items exist ──
    log.info("[STEP 4] Verifying / creating masters in Tally ...")

    masters_ok = True

    if inv["vendor_name"]:
        if not _ensure_ledger(inv["vendor_name"], VENDOR_LEDGER_GROUP, tally_company):
            masters_ok = False
    else:
        log.error("  Cannot create vendor ledger – name is empty")
        masters_ok = False

    if not _ensure_ledger(PURCHASE_LEDGER, PURCHASE_LEDGER_GROUP, tally_company):
        masters_ok = False

    for it in inv["line_items"]:
        if not _ensure_stock_item(it["item_name"], it["unit"], tally_company):
            masters_ok = False

    if not masters_ok:
        log.error("  One or more masters could not be created. Aborting voucher push.")
        sys.exit(1)

    log.info("  ✓ All masters verified/created")

    # ── STEP 5: Push voucher to Tally ───────────────────────────
    log.info("[STEP 5] Pushing item-invoice purchase voucher to Tally ...")
    success = _push_voucher(inv, tally_company)

    if success:
        log.info("=" * 60)
        log.info("✓ Pipeline completed successfully for invoice: %s", invoice_number)
        log.info("  Check TallyPrime Day Book for date: %s-%s-%s",
                 inv["invoice_date"][6:8], inv["invoice_date"][4:6], inv["invoice_date"][:4])
        log.info("  Full log saved to: %s", LOG_FILE.resolve())
        log.info("=" * 60)
    else:
        log.error("=" * 60)
        log.error("✗ Pipeline failed at voucher push for invoice: %s", invoice_number)
        log.error("  Check the saved XML at: invoices_data/tally_xml/%s.xml",
                  invoice_number.replace("/", "-").replace("\\", "-"))
        log.error("  Check the log above for details.")
        log.error("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()