import os
os.environ["DYLD_LIBRARY_PATH"] = "/opt/homebrew/lib"

import sqlite3
from pathlib import Path
from urllib.parse import quote, urlparse
import requests
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams
import io

BASE = Path("/Users/kacquilano/Desktop/city_notification_tracker/stl-meetings/app")
DATA_DIR = BASE / "data"
PDF_DIR = BASE / "pdfs"
DB_PATH = DATA_DIR / "meetings.db"
PROXY_URL = "https://stl-proxy.dan-f8a.workers.dev/?url="

def proxy_get(url, timeout=30):
    return requests.get(PROXY_URL + quote(url, safe=''), timeout=timeout)

def extract_text_from_pdf(pdf_bytes):
    try:
        out = io.StringIO()
        extract_text_to_fp(io.BytesIO(pdf_bytes), out, laparams=LAParams())
        return ' '.join(out.getvalue().split())
    except Exception as e:
        print(f"PDF extraction failed: {e}")
        return None

# Download and extract text from the PDF we found
pdf_url = "https://www.stlouis-mo.gov/government/departments/public-safety/civilian-oversight/civilian-oversight-board/documents/upload/COB-Meeting-Agenda-4-20-2026.pdf"
print("Downloading PDF...")
resp = proxy_get(pdf_url, timeout=60)
print("Status:", resp.status_code)
text = extract_text_from_pdf(resp.content)
if text:
    print(f"Extracted {len(text)} chars")
    print("Sample:", text[:300])
else:
    print("No text extracted")