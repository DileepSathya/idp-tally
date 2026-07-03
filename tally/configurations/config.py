
import re
import xml.etree.ElementTree as ET
from datetime import datetime

def normalize_to_bytes(xml_request):


    # Normalize input to bytes, whatever form it comes in as
    if isinstance(xml_request, ET.ElementTree):
        xml_bytes = ET.tostring(xml_request.getroot(), encoding="utf-8")
    elif isinstance(xml_request, ET.Element):
        xml_bytes = ET.tostring(xml_request, encoding="utf-8")
    elif isinstance(xml_request, str):
        xml_bytes = xml_request.encode("utf-8")
    else:
        xml_bytes = xml_request  # assume already bytes

    return xml_bytes



def clean_tally_xml(xml_text):
    """
    Removes invalid XML control characters that Tally sometimes includes
    in its HTTP response (e.g. &#4;, &#5;, vertical tabs, etc.)
    """
    # Remove illegal XML 1.0 control characters (keep tab, newline, carriage return)
    xml_text = re.sub(
        r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]',
        '',
        xml_text
    )
    # Remove numeric character references to invalid control chars, e.g. &#4;
    xml_text = re.sub(r'&#x?0*[0-8BCEFbcef];', '', xml_text)
    return xml_text






def convert_date_yyyymmdd(raw_date: str) -> str:
    """
    Converts a raw invoice date string into Tally's required YYYYMMDD format.
    Tries multiple common formats since OCR/extraction sources are inconsistent.
    """
    raw_date = raw_date.strip()

    known_formats = [
        "%d-%b-%y",    # 31-Dec-25
        "%d-%b-%Y",    # 31-Dec-2025
        "%d/%m/%Y",    # 31/01/2026
        "%d/%m/%y",    # 31/01/26
        "%d-%m-%Y",    # 31-01-2026
        "%d-%m-%y",    # 31-01-26
        "%Y-%m-%d",    # 2026-01-31 (ISO)
        "%m/%d/%Y",    # 01/31/2026 (US style, check last — ambiguous with %d/%m/%Y)
        "%B %d, %Y",   # January 31, 2026
        "%d %B %Y",    # 31 January 2026
        "%d %b %Y",    # 31 Jan 2026
    ]

    for fmt in known_formats:
        try:
            parsed_date = datetime.strptime(raw_date, fmt)
            return parsed_date.strftime("%Y%m%d")
        except ValueError:
            continue

    # None of the known formats matched
    raise ValueError(f"Unrecognized date format: {raw_date!r}. "
                      f"Tried formats: {known_formats}")



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
