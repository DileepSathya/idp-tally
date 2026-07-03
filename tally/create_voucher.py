import logging
import requests
from tally.configurations.config import (
    clean_tally_xml,
    normalize_to_bytes,
    convert_date_yyyymmdd,
    parse_tally_response,
)

logger = logging.getLogger(__name__)


def ledger_entries_xml(data):
    """Builds the ALLINVENTORYENTRIES.LIST XML block for every line item."""
    line_items = data['gemini']['json']['line_items']
    purchase_ledger = 'Purchase A/c'

    total_invoice_amount = 0.0
    inventory_entries_xml = ""

    for item in line_items:
        print(item)
        quantity = int(item['quantity'])
        rate = float(item['price_per_unit'])
        unit = item.get('unit', 'Nos')

        item_amount = quantity * rate
        total_invoice_amount += item_amount

        formatted_item_amount = f"{item_amount:.2f}"
        formatted_rate = f"{rate:.2f}"
        qty_str = f"{quantity} {unit}"

        inventory_entries_xml += f"""
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{item['service']}</STOCKITEMNAME>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <RATE>{formatted_rate}/{unit}</RATE>
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                            <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            <ACCOUNTINGALLOCATIONS.LIST>
                                <LEDGERNAME>{purchase_ledger}</LEDGERNAME>
                                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                                <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            </ACCOUNTINGALLOCATIONS.LIST>
                        </ALLINVENTORYENTRIES.LIST>"""

    return inventory_entries_xml, total_invoice_amount


def send_template_to_tally(TALLY_URL, path, company_name, data, invoice_number, voucher_type, vendor_name):
    try:
        
        # Read the external template file
        with open(path, "r") as file:
            template_content = file.read()

        # Build inventory block + computed total from line items
        inventory_entries_xml, computed_total = ledger_entries_xml(data)

        # Prefer the extracted total_amount if present, else fall back to computed sum
        raw_total = data['gemini']['json']['total_amount']
        total_amount = f"{float(raw_total):.2f}"

        # Inject Python variables including the INVOICE_NUMBER
        xml_payload = template_content.format(
            COMPANY_NAME=company_name,
            INVOICE_NUMBER=invoice_number,
            VOUCHER_TYPE=voucher_type,
            VOUCHER_DATE=convert_date_yyyymmdd(data['gemini']['json']['invoice_date']),
            PARTY_LEDGER=vendor_name,
            TOTAL_AMOUNT=total_amount,
            INVENTORY_ENTRIES_XML=inventory_entries_xml,
        )

        # Strip control characters that break Tally's XML parser
        xml_payload = clean_tally_xml(xml_payload)

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