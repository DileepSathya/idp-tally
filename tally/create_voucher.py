import logging
import requests
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from tally.configurations.config import (
    clean_tally_xml,
    normalize_to_bytes,
    convert_date_yyyymmdd,
    parse_tally_response,
)

logger = logging.getLogger(__name__)

IGST_ledger = "IGST"


def _safe(value):
    """XML-escapes any value that will be inserted as element text content.
    Prevents raw '&', '<', '>' in extracted invoice text (e.g. 'S & S Traders')
    from breaking the XML document."""
    return xml_escape(str(value))


def ledger_entries_xml(data):
    """Builds the ALLINVENTORYENTRIES.LIST XML block for every line item."""
    line_items = data['gemini']['json']['line_items']
    purchase_ledger = data['gemini']['json'].get('purchase_ledger', 'Purchase A/c')

    total_invoice_amount = 0.0
    inventory_entries_xml = ""

    for item in line_items:
        quantity = float(item['quantity'])
        rate = float(item['price_per_unit'])
        unit = item.get('unit', 'ltr')

        item_amount = quantity * rate
        total_invoice_amount += item_amount

        formatted_item_amount = f"{item_amount:.2f}"
        formatted_rate = f"{rate:.2f}"
        qty_str = f"{quantity:g} {_safe(unit)}"

        inventory_entries_xml += f"""
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{_safe(item['erp_item_name'])}</STOCKITEMNAME>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <RATE>{formatted_rate}/{_safe(unit)}</RATE>
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                            <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            <ACCOUNTINGALLOCATIONS.LIST>
                                <LEDGERNAME>{_safe(purchase_ledger)}</LEDGERNAME>
                                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                                <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            </ACCOUNTINGALLOCATIONS.LIST>
                        </ALLINVENTORYENTRIES.LIST>"""

    return inventory_entries_xml, total_invoice_amount


def tax_entries_xml(data, computed_total):
    """Builds the LEDGERENTRIES.LIST XML blocks with mathematically balanced accounting signs.

    Sign convention used throughout (matches PARTYLEDGERNAME entry in the template):
        ISDEEMEDPOSITIVE = Yes (Debit)  -> AMOUNT is NEGATIVE
        ISDEEMEDPOSITIVE = No  (Credit) -> AMOUNT is POSITIVE
    """
    additional_fields = data['gemini']['json'].get("additional_fields", {})
    blocks = []
    # Running total of tax actually applied, populated by whichever branch
    # (IGST vs CGST+SGST) runs below. This replaces the old bug where the
    # round-off block referenced `igst_amount`/`total_amount` unconditionally,
    # which raised NameError whenever the *other* branch had executed.
    total_tax_amount = 0.0

    # 1. Discount Received (Credit side -> POSITIVE amount, per convention above)
    discount = float(additional_fields.get("discount", 0) or 0)
    if discount > 0:
        discount_ledger = _safe(additional_fields.get("discount_ledger_name", "Discount Received"))
        discount_block = f"""      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{discount_ledger}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE> <!-- Yes + positive amount = ledger balances AND invoice display subtracts it -->
       <AMOUNT>{discount:.2f}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
        blocks.append(discount_block)

    # 2. IGST/Taxes (Debit -> NEGATIVE amount)
    seller_gst = additional_fields.get("seller_gstin", "")[:2]
    buyer_gst = additional_fields.get("buyer_gstin", "")[:2]

    if seller_gst and buyer_gst and seller_gst != buyer_gst:
        igst_rate = float(additional_fields.get("igst_rate", 0) or 0)
        igst_amount = float(additional_fields.get("igst_amount", 0) or 0)

        if igst_amount > 0:
            total_tax_amount += igst_amount
            tally_tax_amount = f"-{igst_amount:.2f}"
            ledger_name = _safe(IGST_ledger)
            block = f"""      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{ledger_name}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE> <!-- Debit -->
       <RATE>{igst_rate:.2f}</RATE>
       <AMOUNT>{tally_tax_amount}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
            blocks.append(block)
    else:
        total_rate = float(additional_fields.get("igst_rate", 0) or 0)
        total_tax_field = float(additional_fields.get("igst_amount", 0) or 0)

        cgst_rate = float(additional_fields.get("cgst_rate", 0) or (total_rate / 2))
        sgst_rate = float(additional_fields.get("sgst_rate", 0) or (total_rate / 2))
        cgst_amount = float(additional_fields.get("cgst_amount", 0) or (total_tax_field / 2))
        sgst_amount = float(additional_fields.get("sgst_amount", 0) or (total_tax_field / 2))

        tax_map = [("CGST", cgst_rate, cgst_amount), ("SGST", sgst_rate, sgst_amount)]

        for tax_type, rate, amount in tax_map:
            if amount > 0:
                total_tax_amount += amount
                tally_tax_amount = f"-{amount:.2f}"
                ledger_name = _safe(tax_type)
                block = f"""      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{ledger_name}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
       <RATE>{rate:.2f}</RATE>
       <AMOUNT>{tally_tax_amount}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
                blocks.append(block)

    # 3. Round Off (Debit -> NEGATIVE amount, Credit -> POSITIVE amount)
    round_off = float(additional_fields.get("round_off", 0) or 0)
    if round_off != 0:
        round_ledger = _safe(additional_fields.get("round_off_ledger_name", "Round Off"))

        # Compare what we've built up (line items - discount + tax) against the
        # invoice's stated total to decide which direction round-off needs to go.
        invoice_total = float(data['gemini']['json'].get('total_amount', 0) or 0)
        fin_cmp_total = computed_total - discount + total_tax_amount

        if fin_cmp_total > invoice_total:
            is_deemed = "Yes"
            tally_round_amount = f"{abs(round_off):.2f}"
        else:
            is_deemed = "No"
            tally_round_amount = f"{abs(round_off):.2f}"

        round_block = f"""      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{round_ledger}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>{is_deemed}</ISDEEMEDPOSITIVE>
       <AMOUNT>{tally_round_amount}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
        blocks.append(round_block)

    return "\n".join(blocks)


def send_template_to_tally(TALLY_URL, path, company_name, data, invoice_number, voucher_type, vendor_name):
    try:
        required = {
            "company_name": company_name,
            "vendor_name": vendor_name,
            "invoice_number": data['gemini']['json']['invoice_number'],
            "voucher_type": voucher_type,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required field(s) before sending to Tally: {missing}")

        with open(path, "r") as file:
            template_content = file.read()

        # Build inventory block
        inventory_entries_xml, cmp_total = ledger_entries_xml(data)

        # Build tax entries block
        tax_entries_string = tax_entries_xml(data, cmp_total)

        # Push the exact parsed amount directly from the JSON payload
        raw_total = data['gemini']['json'].get('total_amount', 0)
        total_amount = f"{float(raw_total):.2f}"  # Formats "11391" to "11391.00"

        # Invoice date comes from the extracted data, not a hardcoded value
        invoice_date = data['gemini']['json'].get('invoice_date')
        if not invoice_date:
            raise ValueError("Missing required field: invoice_date")

        # Inject variables into your XML template
        xml_payload = template_content.format(
            COMPANY_NAME=_safe(company_name),
            INVOICE_NUMBER=_safe(invoice_number),
            VOUCHER_TYPE=_safe(voucher_type),
            VOUCHER_DATE=convert_date_yyyymmdd("2025-07-01"),#invoice_date
            PARTY_LEDGER=_safe(vendor_name),
            TOTAL_AMOUNT=total_amount,  # Pushes exact read value to the Party Ledger Credit side
            INVENTORY_ENTRIES_XML=inventory_entries_xml,
            TAX_ENTRIES_XML=tax_entries_string,
        )

        xml_payload = clean_tally_xml(xml_payload)

        try:
            ET.fromstring(xml_payload)
        except ET.ParseError as e:
            logger.error("Built XML payload is not well-formed: %s", e)
            return

        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        response = requests.post(TALLY_URL, data=normalize_to_bytes(xml_payload), headers=headers)

        if response.status_code == 200:
            logger.info("Raw response from Tally: %s", response.text)
            parse_tally_response(response.text)
        else:
            logger.error("HTTP Error %s: %s", response.status_code, response.text)

    except Exception as e:
        logger.error("Error executing Tally template injection: %s", e)