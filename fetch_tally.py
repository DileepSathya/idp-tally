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
    """Builds the ALLINVENTORYENTRIES.LIST XML block for every line item,
    integrating standard purchase tracking references for existing PO numbers.

    NOTE ON PO MATCHING: Tally resolves an outstanding Purchase Order by the
    combination of (party, order type, order number) - and, for date-based
    disambiguation, the order date. Sending only <ORDERNUMBER> is not enough
    for Tally to recognize this as a reference to an *existing* order; it
    needs <ORDERTYPE> alongside it, and ideally <BASICORDERDATE> if you have
    the PO date available. Without ORDERTYPE, Tally has historically been
    observed to just store the number as inert text rather than linking/
    closing the PO line - which matches the "not getting matched" symptom.

    Also note: Tally matches PO lines by exact STOCKITEMNAME (and often
    unit/godown). If `erp_item_name` doesn't exactly match the stock item
    name used on the PO inside Tally, the number can be present and still
    fail to link.
    """
    json_data = data['gemini']['json']
    line_items = json_data['line_items']
    purchase_ledger = json_data.get('purchase_ledger', 'Purchase A/c')

    additional_fields = json_data.get("additional_fields", {})

    # Purchase Order ID - check top-level first, then additional_fields.
    # If this keeps coming through empty, confirm the exact key name your
    # Gemini extraction schema actually uses (it may not be "po_id").
    po_id = json_data.get("po_id") or additional_fields.get("po_id", "")

    # NOTE: order date (BASICORDERDATE) was tested and confirmed to have no
    # bearing on whether Tally matches the PO - deliberately NOT sending it.
    # Only ORDERTYPE + ORDERNUMBER are sent below.

    if not po_id:
        logger.warning(
            "No po_id found in extracted data (checked top-level 'po_id' and "
            "additional_fields.po_id) - inventory lines will be sent WITHOUT "
            "ORDERDETAILS.LIST, so Tally has nothing to match against."
        )

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

        # Initial inventory object block allocation mapping
        item_xml = f"""
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{_safe(item['erp_item_name'])}</STOCKITEMNAME>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <RATE>{formatted_rate}/{_safe(unit)}</RATE>
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                            <AMOUNT>-{formatted_item_amount}</AMOUNT>"""

        # MATCH PURCHASE ORDER: ORDERTYPE alongside ORDERNUMBER, no date -
        # order date was tested and confirmed not to affect matching.
        if po_id:
            item_xml += f"""
                            <ORDERDETAILS.LIST>
                                <ORDERTYPE>Purchase Order</ORDERTYPE>
                                <ORDERNUMBER>{_safe(po_id)}</ORDERNUMBER>
                            </ORDERDETAILS.LIST>"""

        # Close inventory tags adding financial branch accounts line structures
        item_xml += f"""
                            <ACCOUNTINGALLOCATIONS.LIST>
                                <LEDGERNAME>{_safe(purchase_ledger)}</LEDGERNAME>
                                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                                <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            </ACCOUNTINGALLOCATIONS.LIST>
                        </ALLINVENTORYENTRIES.LIST>"""

        inventory_entries_xml += item_xml

    return inventory_entries_xml, total_invoice_amount


def tax_entries_xml(data, computed_total):
    """Builds the LEDGERENTRIES.LIST XML blocks with mathematically balanced accounting signs.

    Sign convention used throughout (matches PARTYLEDGERNAME entry in the template):
        ISDEEMEDPOSITIVE = Yes (Debit)  -> AMOUNT is NEGATIVE
        ISDEEMEDPOSITIVE = No  (Credit) -> AMOUNT is POSITIVE
    """
    additional_fields = data['gemini']['json'].get("additional_fields", {})
    blocks = []
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
        json_root = data['gemini']['json']
        required = {
            "company_name": company_name,
            "vendor_name": vendor_name,
            "invoice_number": json_root['invoice_number'],
            "voucher_type": voucher_type,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required field(s) before sending to Tally: {missing}")

        with open(path, "r") as file:
            template_content = file.read()

        # Build inventory block (now carries ORDERTYPE + ORDERNUMBER [+ BASICORDERDATE] per line)
        inventory_entries_xml, cmp_total = ledger_entries_xml(data)

        # Build tax entries block
        tax_entries_string = tax_entries_xml(data, cmp_total)

        # Push the exact parsed amount directly from the JSON payload
        raw_total = json_root.get('total_amount', 0)
        total_amount = f"{float(raw_total):.2f}"

        # Invoice date comes from the extracted data
        invoice_date = json_root.get('invoice_date')
        if not invoice_date:
            raise ValueError("Missing required field: invoice_date")

        # Inject formatting keys cleanly into the template structure
        # NOTE: template no longer has a voucher-level {ORDER_NUMBER}/ORDERNUMBER
        # placeholder - the earlier version wired the invoice number into that
        # slot, which conflicted with the real PO number sent per-line below.
        xml_payload = template_content.format(
            COMPANY_NAME=_safe(company_name),
            INVOICE_NUMBER=_safe(invoice_number),
            VOUCHER_TYPE=_safe(voucher_type),
            VOUCHER_DATE=convert_date_yyyymmdd(invoice_date),
            PARTY_LEDGER=_safe(vendor_name),
            TOTAL_AMOUNT=total_amount,
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