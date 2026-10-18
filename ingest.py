"""Send raw Forebet page text to the central ingestion server."""

import logging
import os

import requests


logger = logging.getLogger(__name__)
DEFAULT_INGEST_URL = "http://104.29.193.196:8888/forebet_ingest"


def post_pages(pages, fetched_at):
    url = os.environ.get("INGEST_URL", DEFAULT_INGEST_URL)
    payload = {
        "source": "railway-shifter-reader",
        "fetched_at": fetched_at,
        "pages": {key: page["text"] for key, page in pages.items() if page.get("status") == "ok"},
    }
    if not payload["pages"]:
        logger.warning("No successful pages to ingest")
        return False
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Ingestion POST failed")
        return False
    logger.info("Ingestion POST succeeded for %d pages (HTTP %d)", len(payload["pages"]), response.status_code)
    return True
