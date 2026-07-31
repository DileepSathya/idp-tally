import logging
import os
import sys

from logging_config import setup_logging
setup_logging()

import xml.etree.ElementTree as ET
from tally import tally_details, invoice_data_retriver, validation, create_voucher

missing_stock_items = []
missing_vendors = []


logger = logging.getLogger(__name__)
TALLY_URL = "http://localhost:9000"

logger.info("STAGE-1: Retrieving the company name, ledgers and stock items from tally")

logger.info('reading the xml file')
xml_company_retriever = ET.parse(r"D:\TALLY INTEGRATION\xml_scripts\company_name.xml")
xml_ledgers_list = ET.parse(r"./xml_scripts/ledger_list.xml")
xml_stock_items = ET.parse(r"./xml_scripts/stock_items.xml")
xml_create_voucher = r"./xml_scripts/create_voucher.xml"

logger.info('xml read complete')
"""
logger.info("Retrieving the active company in tally")
company_name = tally_details.active_company(TALLY_URL, xml_company_retriever)
print(company_name)

logger.info("Retrieving ledgers")
ledgers_dict = tally_details.ledgers_retriver(TALLY_URL, xml_ledgers_list)
print(ledgers_dict)

logger.info("Retrieving stock items")
stock_items = tally_details.stock_items(TALLY_URL, xml_stock_items)
print(stock_items)

logger.info("STAGE-1: completed")
"""
logger.info("STAGE-2: Retrieving of invoice from db and validating with Tally data")

MONGO_URI        = os.environ.get("MONGO_URI",                 "mongodb://localhost:27017")
MONGO_DB         = os.environ.get("MONGO_DB",                  "IDP")
MONGO_COLLECTION = os.environ.get("MONGO_INVOICES_COLLECTION", "invoices")

INVOICE_NUMBER = ["TTFPL/25-26/510","KLKA2526-12078"]
#INVOICE_NUMBER = ["TTFPL/25-26/510"]

all_missing = {}

for inv in INVOICE_NUMBER:

    invoice_number = inv.strip()

    if not invoice_number:
        logger.error("Invoice number is empty")
        continue

    logger.info("Connecting to MongoDB")

    data_json = invoice_data_retriver.db_connection(
        MONGO_URI,
        MONGO_DB,
        MONGO_COLLECTION,
        invoice_number
    )
    create_voucher.send_template_to_tally(
        TALLY_URL=TALLY_URL,
        path=xml_create_voucher,
        company_name="My Home Jewel Apartments",
        data=data_json,
        invoice_number=invoice_number,
        voucher_type="Purchase",
        vendor_name=data_json['gemini']['json']['additional_fields']['erp_vendor_name'],
    )
"""
    result = validation.validate_invoice(
        data_json,
        company_name,
        ledgers_dict,
        stock_items,
        invoice_number,
        {}
    )

    all_missing.update(result)

    # Get the actual vendor name for THIS invoice from the extracted data,
    # not from the missing-items validation report (that's a different concept
    # entirely — it tracks vendors/items that failed validation against Tally).
    
    vendor_name = data_json['gemini']['json']['seller']

    if not vendor_name:
        logger.error(
            "No vendor_name found in extracted data for invoice %s — skipping push to Tally",
            invoice_number
        )
        continue
"""
"""    create_voucher.send_template_to_tally(
        TALLY_URL=TALLY_URL,
        path=xml_create_voucher,
        company_name="My Home Jewel Apartments___ ameya",
        data=data_json,
        invoice_number=invoice_number,
        voucher_type="Purchase",
        vendor_name=vendor_name,
    )"""


"""for details in all_missing.values():
    missing_stock_items.extend(details.get("stock_items", []))

    vendor = details.get("Vendor_name")
    if vendor:
        missing_vendors.append(vendor)

print(missing_stock_items)
print(missing_vendors)"""