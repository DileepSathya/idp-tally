import requests
import xml.etree.ElementTree as ET

# 1. Configuration
TALLY_URL = "http://localhost:9000"
TEMPLATE_FILE = r"./xml_scripts/create_voucher.xml"

# 2. Header Details
COMPANY_NAME = "My Home Jewel Apartments"
INVOICE_NUMBER = "KLKA2526-12078"
VOUCHER_DATE = "20260701"                     # YYYYMMDD
VOUCHER_TYPE = "Purchase"
PARTY_LEDGER = "Kre38 Labs Pvt Ltd"   # Supplier / Creditor ledger name (must exist in Tally)
PURCHASE_LEDGER = "Purchase A/c"              # Accounting allocation ledger for items (must exist in Tally)

# 3. Stock Items Data (Particulars)
stock_items = [
    {"item_name": "Logitech Mouse", "qty": 10, "rate": 500.00, "unit": "Nos"},
    {"item_name": "Dell Keyboard", "qty": 5, "rate": 1200.00, "unit": "Nos"}
]

# 4. Process Inventory and Calculate Totals
total_invoice_amount = 0.0
inventory_entries_xml = ""

for item in stock_items:
    item_amount = item['qty'] * item['rate']
    total_invoice_amount += item_amount

    formatted_item_amount = f"{item_amount:.2f}"
    formatted_rate = f"{item['rate']:.2f}"
    qty_str = f"{item['qty']} {item['unit']}"

    inventory_entries_xml += f"""
                        <ALLINVENTORYENTRIES.LIST>
                            <STOCKITEMNAME>{item['item_name']}</STOCKITEMNAME>
                            <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                            <RATE>{formatted_rate}/{item['unit']}</RATE>
                            <ACTUALQTY>{qty_str}</ACTUALQTY>
                            <BILLEDQTY>{qty_str}</BILLEDQTY>
                            <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            <ACCOUNTINGALLOCATIONS.LIST>
                                <LEDGERNAME>{PURCHASE_LEDGER}</LEDGERNAME>
                                <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
                                <AMOUNT>-{formatted_item_amount}</AMOUNT>
                            </ACCOUNTINGALLOCATIONS.LIST>
                        </ALLINVENTORYENTRIES.LIST>"""

TOTAL_AMOUNT = f"{total_invoice_amount:.2f}"
print(f"Calculated Total Invoice Value: {TOTAL_AMOUNT}")


def parse_tally_response(xml_text: str):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"Could not parse Tally response as XML: {e}")
        return

    found_error = False
    for elem in root.iter():
        tag = elem.tag.upper()
        if tag in ("LINEERROR", "ERROR", "EXCEPTION") and elem.text:
            print(f"Tally reported: {elem.tag} -> {elem.text.strip()}")
            found_error = True

    exceptions = root.findtext("EXCEPTIONS")
    created = root.findtext("CREATED")

    if exceptions and exceptions != "0" and not found_error:
        print("EXCEPTIONS > 0 but no LINEERROR text included in the response.")
        print("Check Tally's GUI for a popup, or Display > Exception Reports.")

    if created and created != "0":
        print(f"Voucher(s) created successfully: {created}")


def send_item_invoice_to_tally():
    try:
        with open(TEMPLATE_FILE, "r") as file:
            template_content = file.read()

        xml_payload = template_content.format(
            COMPANY_NAME=COMPANY_NAME,
            INVOICE_NUMBER=INVOICE_NUMBER,
            VOUCHER_TYPE=VOUCHER_TYPE,
            VOUCHER_DATE=VOUCHER_DATE,
            PARTY_LEDGER=PARTY_LEDGER,
            TOTAL_AMOUNT=TOTAL_AMOUNT,
            INVENTORY_ENTRIES_XML=inventory_entries_xml
        )

        # Always save the exact payload sent, for debugging / manual import
        with open("last_payload.xml", "w", encoding="utf-8") as f:
            f.write(xml_payload)
        print("\nSaved outgoing payload to last_payload.xml")

        headers = {'Content-Type': 'text/xml; charset=utf-8'}
        response = requests.post(TALLY_URL, data=xml_payload, headers=headers)

        if response.status_code == 200:
            print("\nRaw response from Tally:")
            print(response.text)
            print("\nParsed result:")
            parse_tally_response(response.text)
        else:
            print(f"HTTP Error: {response.status_code}")
            print(response.text)

    except FileNotFoundError:
        print(f"Error: Check if '{TEMPLATE_FILE}' exists.")
    except requests.exceptions.ConnectionError:
        print("Connection failed. Ensure Tally is open and port 9000 is active.")


if __name__ == "__main__":
    send_item_invoice_to_tally()