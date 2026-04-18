import requests
import re
import json

def find_ids():
    url = "https://polymarket.com/event/btc-updown-5m-1776546600"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    print(f"Fetching {url}...")
    try:
        r = requests.get(url, headers=headers, timeout=10)
        # Search for seriesId in the raw HTML
        sid = re.search(r'"seriesId":(\d+)', r.text)
        mid = re.search(r'"id":"(\d+)"', r.text)
        
        # Search for it inside double quotes (JSON style)
        if not sid:
            sid = re.search(r'\"seriesId\":(\d+)', r.text)
        
        print(f"SERIES_ID={sid.group(1) if sid else 'NOT_FOUND'}")
        print(f"MARKET_ID={mid.group(1) if mid else 'NOT_FOUND'}")
        
    except Exception as e:
        print(f"ERROR: {str(e)}")

if __name__ == "__main__":
    find_ids()
