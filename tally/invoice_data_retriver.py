from pymongo import MongoClient
import logging
import sys
logger = logging.getLogger(__name__)
def db_connection(MONGO_URI,MONGO_DB,MONGO_COLLECTION,invoice_number):
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.server_info()   # force connection check
        coll = client[MONGO_DB][MONGO_COLLECTION]
        logger.info("  Connected to DB '%s', collection '%s'", MONGO_DB, MONGO_COLLECTION)
    except Exception as exc:
        logger.error("  MongoDB connection failed: %s", exc)
        sys.exit(1)
    logger.info("[STEP 1] Searching for invoice '%s' ...", invoice_number)
    doc = coll.find_one(
        {"$or": [
            {"gemini.json.invoice_number": invoice_number},
            {"gemini.json.invoice":        invoice_number},
        ]},
        sort=[("_id", -1)],
    )

    if not doc:
        logger.error("  No data found for invoice number '%s'. Exiting.", invoice_number)
        sys.exit(1)

    
    logger.info("  ✓ Invoice found in MongoDB (document _id: %s)", doc.get("_id"))
    return doc