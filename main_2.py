import logging
import os
import sys

from logging_config import setup_logging
setup_logging()

import xml.etree.ElementTree as ET
from tally import invoice_data_retriver

logger = logging.getLogger(__name__)
TALLY_URL = "http://localhost:9000"

logger.info("STAGE-1: Retrieving the company name, ledgers and stock items from tally")

logger.info("STAGE-2: Retrieving of invoice from db and validating with Tally data")

MONGO_URI        = os.environ.get("MONGO_URI",                 "mongodb://localhost:27017")
MONGO_DB         = os.environ.get("MONGO_DB",                  "IDP")
MONGO_COLLECTION = os.environ.get("MONGO_INVOICES_COLLECTION", "invoices")

INVOICE_NUMBER = ["KLKA2526-12078","36230377"]

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