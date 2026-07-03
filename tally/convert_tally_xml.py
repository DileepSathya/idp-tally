"""
tally_pipeline.py
=================
Full pipeline:
  1. Fetch invoice from MongoDB by invoice number
  2. Convert to Tally-compatible structure
  3. Resolve target Tally company from TALLY_COMPANY env var or FY-based auto-name
  4. Check each ledger in that company – create if missing
  5. Push the purchase voucher to Tally
  6. Log every step, warning, and error to console + tally_pipeline.log

Prerequisites:
  - TallyPrime must be running with the target company already open in the UI.
  - Set TALLY_COMPANY in .env to the exact company name shown in TallyPrime's
    title bar.  If not set, the pipeline auto-derives the name from the invoice
    date as {TALLY_COMPANY_PREFIX}_FY{yy}{yy+1} (e.g. IDP_FY2526).

Usage:
  Set INVOICE_NUMBER below, then run:
      pip install pymongo python-dotenv requests
      python tally_pipeline.py
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
#"KLKA2526-12078" "36230377"  "SSS/25-26/00832"
INVOICE_NUMBER = "KLKA2526-12078" # <── set your invoice number here

TALLY_URL          = "http://192.168.29.237:9000"   # Tally HTTP server
TALLY_TIMEOUT      = 10                        # seconds
TALLY_LONG_TIMEOUT = 120                       # company list / heavy exports

# TALLY_COMPANY: exact name of the company already open in TallyPrime (required).
# Visible in TallyPrime's title bar.  If not set, name is auto-derived from the
# invoice's Indian financial year as {prefix}_FY{yy}{yy+1} (e.g. IDP_FY2526).
TALLY_COMPANY_ENV    = os.environ.get("TALLY_COMPANY", "").strip()
TALLY_COMPANY_PREFIX = os.environ.get("TALLY_COMPANY_PREFIX", "IDP").strip() or "IDP"

# INVOICE_DATE_OVERRIDE: set this (YYYY-MM-DD) to force a specific invoice date
# when Gemini has extracted a wrong date from the document.
# Example: INVOICE_DATE_OVERRIDE=2025-12-01
INVOICE_DATE_OVERRIDE = os.environ.get("INVOICE_DATE_OVERRIDE", "").strip()

# Dates this many days in the future are flagged as implausible for an invoice.
INVOICE_DATE_MAX_FUTURE_DAYS = int(os.environ.get("INVOICE_DATE_MAX_FUTURE_DAYS", "30"))

# MongoDB (can also be set via .env)
MONGO_URI        = os.environ.get("MONGO_URI",                 "mongodb://localhost:27017")
MONGO_DB         = os.environ.get("MONGO_DB",                  "IDP")
MONGO_COLLECTION = os.environ.get("MONGO_INVOICES_COLLECTION", "invoices")

# Ledger → Tally group mapping
LEDGER_GROUP_MAP: dict[str, str] = {
    "vendor":   "Sundry Creditors",
    "purchase": "Purchase Accounts",
    "expense":  "Indirect Expenses",
}

# ─────────────────────────────────────────────
#  LOGGING SETUP
# ─────────────────────────────────────────────
LOG_FILE = Path("tally_pipeline.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tally_pipeline")


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
    """
    Return date as YYYYMMDD for Tally.

    Format priority (most-specific / least-ambiguous first):
      - 4-digit year formats before 2-digit year formats so '25' is never
        misread as a year when the field is actually a day.
      - Named-month formats (%b) before numeric ones so '01-Jan-25' is
        matched before a numeric pattern can misinterpret the parts.
      - %d-%m-%Y is explicitly included; the original list only had
        %Y-%m-%d (ISO) which does NOT match 'DD-MM-YYYY' order.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    formats = [
        # Named month, 4-digit year  (e.g. "01-Dec-2025", "01 Dec 2025")
        "%d-%b-%Y", "%d %b %Y",
        # Named month, 2-digit year  (e.g. "1-Dec-25")
        "%d-%b-%y", "%d %b %y",
        # ISO 8601                    (e.g. "2025-12-01")
        "%Y-%m-%d",
        # Numeric, 4-digit year, slash (e.g. "01/12/2025")
        "%d/%m/%Y",
        # Numeric, 4-digit year, dash  (e.g. "01-12-2025")
        "%d-%m-%Y",
        # Numeric, 2-digit year, slash (e.g. "01/12/25")
        "%d/%m/%y",
        # Numeric, 2-digit year, dash  (e.g. "01-12-25")
        "%d-%m-%y",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            result = parsed.strftime("%Y%m%d")
            log.debug("Date '%s' matched format '%s' → %s", text, fmt, result)

            # Warn when the parsed date is suspiciously far in the future —
            # a common symptom of Gemini misreading a 2-digit year.
            delta_days = (parsed - datetime.today()).days
            if delta_days > INVOICE_DATE_MAX_FUTURE_DAYS:
                log.warning(
                    "Invoice date '%s' parsed as %s which is %d days in the future. "
                    "This may be a Gemini extraction error. "
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
    """
    Compact voucher reference for Tally XML fields.

    Tally mis-parses FY-style numbers like TTFPL/25-26/510 (or TTFPL-25-26-510)
    as date components and returns 'Voucher date is missing'. Strip separators.
    """
    s = str(value or "").strip()
    return re.sub(r"[/\\\-]+", "", s)


def _map_line_items(g: dict) -> list[dict]:
    src = g.get("line_items") or []
    out = []
    if isinstance(src, list):
        for li in src:
            if not isinstance(li, dict):
                continue
            qty        = _to_float(li.get("quantity") or li.get("qty"))
            unit_price = _to_float(li.get("unit_price") or li.get("price_per_unit") or li.get("rate"))
            line_total = _to_float(li.get("line_total") or li.get("amount"))

            # Warn when we cannot derive a meaningful amount for this line
            if line_total is None and (qty is None or unit_price is None):
                desc = li.get("description") or li.get("service") or "<unknown>"
                log.warning(
                    "Line item '%s' has no line_total and missing qty/unit_price – "
                    "amount will be 0; skipping this item.",
                    desc,
                )
                continue

            out.append({
                "description": _safe_str(li.get("description") or li.get("service")),
                "hsn":         _safe_str(li.get("hsn") or li.get("hsn_number") or li.get("HSN")),
                "quantity":    qty,
                "unit_price":  unit_price,
                "tax_pct":     _to_float(li.get("tax_pct") or li.get("tax_rate")),
                "line_total":  line_total,
            })
    return out


def _convert_doc(doc: dict) -> dict:
    """Convert raw MongoDB document to intermediate invoice dict."""
    raw_g = (doc.get("gemini") or {}).get("json") or {}

    # gemini.json is sometimes stored as a JSON string rather than a dict.
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

    # Apply date override if Gemini extracted a wrong date (e.g. future date).
    if INVOICE_DATE_OVERRIDE:
        override_normalized = _normalize_date(INVOICE_DATE_OVERRIDE)
        if override_normalized:
            log.info(
                "INVOICE_DATE_OVERRIDE applied: replacing '%s' with '%s'",
                result["invoice_date"],
                override_normalized,
            )
            result["invoice_date"] = override_normalized
        else:
            log.warning(
                "INVOICE_DATE_OVERRIDE='%s' could not be parsed – ignoring override.",
                INVOICE_DATE_OVERRIDE,
            )

    return result


# ─────────────────────────────────────────────
#  XML BUILDERS
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
    """Export request – returns ledger info if it exists."""
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


def _build_voucher_xml(inv: dict, tally_company: str) -> str:
    """
    Build a Purchase voucher XML from the converted invoice dict.

    Tally sign convention for Purchase vouchers:
      - Vendor (Sundry Creditor) = CREDIT side → negative amount, ISDEEMEDPOSITIVE=No
      - Each line item (Purchase/Expense ledger) = DEBIT side → positive amount, ISDEEMEDPOSITIVE=Yes
      - Debit total MUST equal Credit total (i.e. sum of line items == total_amount)

    FIX: When line item totals don't add up to total_amount, we force-balance
    by adjusting the last line item so Tally never sees an unbalanced voucher
    (which causes EXCEPTIONS=1 with CREATED=0 — a silent rejection).
    """
    date = inv["invoice_date"] or datetime.today().strftime("%Y%m%d")
    # Tally misinterprets '/' in voucher/reference fields as date separators,
    # producing a misleading "Voucher date is missing" LINEERROR. Sanitize all
    # voucher identifiers; keep the original number in narration for audit.
    vch_number_raw = _safe_str(inv["invoice_number"])
    vch_number_safe = _xml_esc(_sanitize_voucher_ref(vch_number_raw))
    narration_raw = _safe_str(inv.get("narration"))
    if vch_number_raw and _sanitize_voucher_ref(vch_number_raw) != vch_number_raw:
        audit = f"SourceInvoiceRef={vch_number_raw}"
        narration_raw = f"{narration_raw} | {audit}" if narration_raw else audit
    vendor = _xml_esc(inv["vendor_name"])
    ce = _xml_esc(tally_company)
    total = inv["total_amount"] or 0.0
    narration = _xml_esc(narration_raw)

    # ── Compute per-line amounts ──────────────────────────────────────────
    line_amounts: list[float] = []
    for li in inv["line_items"]:
        line_amt = li["line_total"]
        if line_amt is None:
            qty        = li["quantity"] or 0
            unit_price = li["unit_price"] or 0
            line_amt   = round(qty * unit_price, 2)
        line_amounts.append(abs(line_amt))

    # ── Balance check & auto-correction ──────────────────────────────────
    # If lines don't sum to total, force-adjust the last line so Tally
    # doesn't silently reject with EXCEPTIONS=1 / CREATED=0.
    if line_amounts:
        lines_sum = round(sum(line_amounts), 2)
        total_abs = round(abs(total), 2)
        if lines_sum != total_abs:
            diff = round(total_abs - lines_sum, 2)
            log.warning(
                "Voucher imbalance detected: line items sum to %.2f but total_amount is %.2f "
                "(diff=%.2f). Auto-adjusting last line item to balance.",
                lines_sum, total_abs, diff,
            )
            line_amounts[-1] = round(line_amounts[-1] + diff, 2)
            if line_amounts[-1] < 0:
                log.error(
                    "Auto-adjustment produced a negative line amount (%.2f). "
                    "Check your invoice data – voucher may still be rejected.",
                    line_amounts[-1],
                )
    else:
        # No line items – create a single catch-all purchase entry
        log.warning("No line items found – creating a single catch-all Purchase entry for total amount.")
        line_amounts = [abs(total)]
        inv["line_items"] = [{"description": "Purchase", "hsn": "", "quantity": None,
                               "unit_price": None, "tax_pct": None, "line_total": abs(total)}]

    # ── Build ledger entry XML (demo.py / import_simple_purchase.py convention) ──
    entries = []

    # Party ledger: credit, positive amount, bill allocation
    entries.append(f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{vendor}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
              <AMOUNT>{abs(total):.2f}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <NAME>{vch_number_safe}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>{abs(total):.2f}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>""")

    # Purchase/expense ledgers: debit, negative amounts
    for li, amt in zip(inv["line_items"], line_amounts):
        desc = _xml_esc(li["description"] or "Purchase")
        entries.append(f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{desc}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
              <ISPARTYLEDGER>No</ISPARTYLEDGER>
              <AMOUNT>-{amt:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>""")

    entries_xml = "\n".join(entries)

    log.debug(
        "Voucher balance: credit=%.2f  debit=%.2f",
        abs(total), sum(line_amounts),
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
          <VOUCHER VCHTYPE="Purchase" ACTION="Create" OBJVIEW="Accounting Voucher View">
            <PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
            <DATE>{date}</DATE>
            <EFFECTIVEDATE>{date}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
            <PARTYLEDGERNAME>{vendor}</PARTYLEDGERNAME>
            <REFERENCE>{vch_number_safe}</REFERENCE>
            <NARRATION>{narration}</NARRATION>
            <ISOPTIONAL>No</ISOPTIONAL>
            <ISINVOICE>No</ISINVOICE>
{entries_xml}
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
    """Alternate YYYYMMDD dates to try when Tally rejects the invoice date."""
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
    """
    Parse key counters from a Tally <RESPONSE> block.
    Returns a dict with keys: created, altered, errors, exceptions, has_lineerror.
    """
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
    """
    Return the Tally company name to use for this invoice.

    Priority:
      1. TALLY_COMPANY env var — use exactly as set.
      2. Auto-derive from the invoice date as {prefix}_FY{yy}{yy+1}.
         e.g. an invoice dated Dec 2025 → IDP_FY2526.

    No network call is made here. TallyPrime must already have the target
    company open; the ledger and voucher requests will fail with a meaningful
    LINEERROR if the wrong company is active.
    """
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
    """
    Check if a ledger exists by querying Tally.
    Looks for the ledger name wrapped in its exact XML <NAME> tag to avoid
    false positives from substring matches.
    """
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
    """Create ledger in Tally if it doesn't already exist. Returns True on success."""
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

    # Unexpected response — log it but don't abort
    log.warning(
        "  Ledger '%s': unexpected Tally response (created=0, errors=0). "
        "Treating as success. Response: %s",
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
            "Tally returned EXCEPTIONS=%d with CREATED=0 – voucher was NOT saved. "
            "Check the saved XML at: %s",
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

    log.error(
        "Voucher push returned CREATED=0 with no explicit error. Full response:\n%s", resp,
    )
    return False


def _push_voucher(inv: dict, tally_company: str) -> bool:
    """
    Build and push the purchase voucher to Tally.

    Retries with alternate voucher dates when Tally rejects the invoice date
    (common for FY-style references and certain calendar dates in TallyPrime).
    """
    log.info("Building voucher XML for invoice '%s'", inv["invoice_number"])

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
                voucher_date,
                primary_date,
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
                    voucher_date,
                    primary_date,
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
    log.info("Starting Tally pipeline for invoice: %s", invoice_number)
    log.info("=" * 60)

    # ── STEP 1: Fetch from MongoDB ──────────────────────────────
    log.info("[STEP 1] Connecting to MongoDB at %s ...", MONGO_URI)
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()   # force connection check
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

        # Warn about missing critical fields
        if not inv["vendor_name"]:
            log.warning("  Vendor name is empty – check MongoDB document structure")
        if not inv["invoice_date"]:
            log.warning("  Invoice date missing – will use today's date")
        if not inv["total_amount"]:
            log.warning("  Total amount is 0 or missing")
        if not inv["line_items"]:
            log.warning("  No line items found – a catch-all Purchase entry will be used")

    except Exception as exc:
        log.error("  Document conversion failed: %s", exc)
        sys.exit(1)

    # Save intermediate JSON for reference
    # FIX: sanitize '/' in invoice number so it doesn't create wrong subdirectories
    safe_inv_filename = invoice_number.replace("/", "-").replace("\\", "-")
    json_path = Path("invoices_data") / "tally_json" / f"{safe_inv_filename}_converted.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(inv, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("  Converted JSON saved to: %s", json_path)

    # ── STEP 3: Company selection ───────────────────────────────
    log.info("[STEP 3] Resolving target Tally company name ...")
    tally_company = _resolve_tally_company(inv)
    log.info("  Using Tally company: '%s'", tally_company)

    # ── STEP 4: Ensure all ledgers exist in Tally ───────────────
    log.info("[STEP 4] Verifying / creating ledgers in Tally ...")

    ledgers_ok = True

    # Vendor ledger
    if inv["vendor_name"]:
        ok = _ensure_ledger(inv["vendor_name"], LEDGER_GROUP_MAP["vendor"], tally_company)
        if not ok:
            ledgers_ok = False
    else:
        log.error("  Cannot create vendor ledger – name is empty")
        ledgers_ok = False

    # Line-item ledgers (one ledger per description)
    for li in inv["line_items"]:
        desc = li["description"]
        if not desc:
            log.warning("  Line item has no description – skipping ledger creation for this item")
            continue
        ok = _ensure_ledger(desc, LEDGER_GROUP_MAP["purchase"], tally_company)
        if not ok:
            ledgers_ok = False

    if not ledgers_ok:
        log.error("  One or more ledgers could not be created. Aborting voucher push.")
        sys.exit(1)

    log.info("  ✓ All ledgers verified/created")

    # ── STEP 5: Push voucher to Tally ───────────────────────────
    log.info("[STEP 5] Pushing purchase voucher to Tally ...")
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