import requests
import re
import json

def find_ids():
    url = "https://polymarket.com/event/btc-updown-5m-1776546600"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    print(f"Fetching {url}...")
    r = requests.get(url, headers=headers)
    
    # Method 1: Regex
    sid_match = re.search(r'"seriesId":(\d+)', r.text)
    eid_match = re.search(r'"eventId":(\d+)', r.text)
    gid_match = re.search(r'"groupId":(\d+)', r.text)
    
    sid = sid_match.group(1) if sid_match else "NONE"
    eid = eid_match.group(1) if eid_match else "NONE"
    gid = gid_match.group(1) if gid_match else "NONE"
    
    print(f"Found via Regex: Series={sid}, Event={eid}, Group={gid}")
    
    # Method 2: Extract Next Data
    next_data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
    if next_data:
        data = json.loads(next_data.group(1))
        # Look for the market in the nested JSON
        # This is more reliable if IDs are deep
        print("NEXT_DATA successfully extracted.")
    
if __name__ == "__main__":
    find_ids()
