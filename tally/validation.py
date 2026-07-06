import logging
import sys

logger = logging.getLogger(__name__)


def company_validation(data, tally_company_name):
    buyer_company = data['gemini']['json']['buyer']

    if buyer_company in [
        "MY HOME JEWEL FLAT OWNERS MAINTENANCE MUTUALIY AIDED CO-OP SOCIETY LIMITED",
        "M/s.MY HOME JEWEL FLAT OWNERS MAINTENANCE MUTUALLY AIDED COOPERATIVE SOCIETYLTD"
    ]:
        buyer_company = tally_company_name

    if buyer_company == tally_company_name:
        logger.info("Company is same and active in tally")
    else:
        logger.error(f"Company '{buyer_company}' is not active in Tally")
        sys.exit(1)


def vendor_validation(data, ledger_dict, not_in_tally):
    vendor = data['gemini']['json']['seller']

    creditors = ledger_dict.get("Sundry Creditors", [])

    if vendor in creditors:
        logger.info("Vendor exists")
    else:
        logger.info(f"Vendor '{vendor}' not found")
        not_in_tally["Vendor_name"] = vendor


def stock_item_validation(data, stock_list, not_in_tally):
    missing_items = []

    for item in data['gemini']['json']['line_items']:
        if item["service"] not in stock_list:
            missing_items.append(item["service"])

    if missing_items:
        logger.info(f"Missing stock items : {missing_items}")
        not_in_tally["stock_items"] = missing_items


def validate_invoice(
    data,
    tally_company_name,
    ledger_dict,
    stock_list,
    invoice_number,
    not_in_tally
):

    company_validation(data, tally_company_name)
    vendor_validation(data, ledger_dict, not_in_tally)
    stock_item_validation(data, stock_list, not_in_tally)

    return {invoice_number: not_in_tally}