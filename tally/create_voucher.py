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
        quantity = int(item['quantity'])
        rate = float(item['price_per_unit'])
        unit = item.get('unit', 'Nos')

        item_amount = quantity * rate
        total_invoice_amount += item_amount

        formatted_item_amount = f"{item_amount:.2f}"
        formatted_rate = f"{rate:.2f}"
        qty_str = f"{quantity} {_safe(unit)}"

        inventory_entries_xml += f"""
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{_safe(item['service'])}</STOCKITEMNAME>
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


def send_template_to_tally(TALLY_URL, path, company_name, data, invoice_number, voucher_type, vendor_name):
    try:
        # Fail fast on missing required fields instead of letting Tally reject with a cryptic error
        required = {
            "company_name": company_name,
            "vendor_name": vendor_name,
            "invoice_number": invoice_number,
            "voucher_type": voucher_type,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required field(s) before sending to Tally: {missing}")

        # Read the external template file
        with open(path, "r") as file:
            template_content = file.read()

        # Build inventory block + computed total from line items
        inventory_entries_xml, computed_total = ledger_entries_xml(data)

        # Prefer the extracted total_amount if present, else fall back to computed sum
        raw_total = data['gemini']['json'].get('total_amount', computed_total)
        total_amount = f"{float(raw_total):.2f}"

        # Inject Python variables including the INVOICE_NUMBER
        # All text values are XML-escaped since they originate from OCR-extracted
        # invoice data that may contain '&', '<', '>' (e.g. "S & S Traders").
        xml_payload = template_content.format(
            COMPANY_NAME=_safe(company_name),
            INVOICE_NUMBER=_safe(invoice_number),
            VOUCHER_TYPE=_safe(voucher_type),
            VOUCHER_DATE= convert_date_yyyymmdd("2026-07-01"), #convert_date_yyyymmdd(data['gemini']['json']['invoice_date']),
            PARTY_LEDGER=_safe(vendor_name),
            TOTAL_AMOUNT=total_amount,
            INVENTORY_ENTRIES_XML=inventory_entries_xml,
        )

        # Strip control characters that break Tally's XML parser
        xml_payload = clean_tally_xml(xml_payload)

        # Validate locally before sending — catches malformed XML immediately
        # with a specific line/column, instead of Tally's generic
        # "Unknown Request, cannot be processed" response.
        try:
            ET.fromstring(xml_payload)
        except ET.ParseError as e:
            logger.error("Built XML payload is not well-formed: %s", e)
            logger.debug("Offending payload:\n%s", xml_payload)
            return

        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        response = requests.post(TALLY_URL, data=normalize_to_bytes(xml_payload), headers=headers)

        if response.status_code == 200:
            logger.info("Raw response from Tally: %s", response.text)
            parse_tally_response(response.text)
        else:
            logger.error("HTTP Error %s: %s", response.status_code, response.text)

    except FileNotFoundError:
        logger.error("Template file not found: %s", path)
    except requests.exceptions.ConnectionError:
        logger.error("Connection failed. Ensure Tally is open and port 9000 is active.")
    except KeyError as e:
        logger.error("Missing expected key in data payload: %s", e)
    except ValueError as e:
        logger.error("Validation error: %s", e)