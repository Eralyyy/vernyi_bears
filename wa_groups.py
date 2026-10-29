"""List your WhatsApp groups with their IDs (needed for WA_GROUP_ID).

    GREENAPI_ID=... GREENAPI_TOKEN=... python3 wa_groups.py
"""
import os

import requests

url = f"https://api.green-api.com/waInstance{os.environ['GREENAPI_ID']}/getContacts/{os.environ['GREENAPI_TOKEN']}"
for c in requests.get(url, timeout=30).json():
    if c.get("type") == "group" or str(c.get("id", "")).endswith("@g.us"):
        print(c["id"], "-", c.get("name"))
