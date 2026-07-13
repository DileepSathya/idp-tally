"""
Here we will return the comapany name list of ledgers and stock items
"""

import xml.etree.ElementTree as ET
import requests
import logging
from tally.configurations.config import clean_tally_xml,normalize_to_bytes
logger = logging.getLogger(__name__)




def active_company(TALLY_URL,xml_request):
    """
    xml_request: either an ET.Element, ET.ElementTree, or an XML string/bytes.
    """


    headers = {"Content-Type": "text/xml;charset=utf-8"}
    response = requests.post(TALLY_URL, data=normalize_to_bytes(xml_request), headers=headers)
    print(response)
   

    if response.status_code == 200:
        root = ET.fromstring(response.content)
        
        company_tag = root.find(".//COMPANY/NAME")


        if company_tag is not None and company_tag.text:
            company_name=company_tag.text.strip()
            
        else:
            print("No active company found. Make sure a company is opened in Tally.")
    else:
        print(f"Failed to connect. HTTP Status Code: {response.status_code}")
    return company_name


def ledgers_retriver(TALLY_URL,xml_request):
    ledger_dict = {}
    try:
        headers = {"Content-Type": "text/xml;charset=utf-8"}
        response = requests.post(TALLY_URL, data=normalize_to_bytes(xml_request), headers=headers)

        if response.status_code == 200:
            raw_text = response.content.decode("utf-8", errors="replace")
            cleaned_text = clean_tally_xml(raw_text)
            root = ET.fromstring(cleaned_text)

            ledgers = root.findall(".//LEDGER")

            if ledgers:
                for ledger in ledgers:
                    name = ledger.get("NAME", "Unknown").strip()

                    parent_tag = ledger.find("PARENT")
                    parent = parent_tag.text.strip() if parent_tag is not None and parent_tag.text else "None"

                    # Group ledger names under their parent as a list
                    ledger_dict.setdefault(parent, []).append(name)

            else:
                print("No ledgers found. Ensure a company is currently open in Tally.")

        else:
            print(f"Failed to connect. HTTP Status Code: {response.status_code}")

    except requests.exceptions.ConnectionError:
        print("Error: Cannot connect to Tally. Verify Tally is running on Port 9000.")
    except Exception as e:
        print(f"An error occurred: {e}")

    return ledger_dict

def stock_items(TALLY_URL,xml_request):
    stock_fin_list=[]
    headers = {"Content-Type": "text/xml;charset=utf-8"}
    response = requests.post(TALLY_URL, data=normalize_to_bytes(xml_request), headers=headers)

    if response.status_code == 200:
            # Decode and clean before parsing
        raw_text = response.content.decode("utf-8", errors="replace")

        cleaned_text = clean_tally_xml(raw_text)

        root = ET.fromstring(cleaned_text)

        stock_list = root.findall(".//STOCKITEM")
        if stock_list:

            for item in stock_list:
                name=item.get("NAME", "Unknown").strip()
                parent_tag = item.find("PARENT")
                parent = parent_tag.text.strip() if parent_tag is not None and parent_tag.text else "None"
                unit_tag = item.find("BASEUNITS")
                unit = unit_tag.text.strip() if unit_tag is not None and unit_tag.text else "N/A"

                stock_fin_list.append(name)
            
        else:
            print("No stock items found in tally")

    else:
        print(f"Failed to connect. HTTP Status Code: {response.status_code}")
    return stock_fin_list
