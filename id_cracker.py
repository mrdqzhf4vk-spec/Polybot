import requests
import re
import json

def crack_id():
    url = "https://polymarket.com/crypto"
    headers = {"User-Agent": "Mozilla/5.0"}
    print(f"Scanning {url} for Master IDs...")
    
    try:
        r = requests.get(url, headers=headers, timeout=15)
        # Find all series IDs
        sids = set(re.findall(r'"seriesId":(\d+)', r.text))
        print(f"Detected {len(sids)} unique Series IDs.")
        
        matches = []
        for sid in sids:
            # Query Gamma for the title of this series
            api_url = f"https://gamma-api.polymarket.com/markets"
            params = {"seriesId": sid, "limit": 1}
            r_api = requests.get(api_url, params=params, timeout=5)
            data = r_api.json()
            
            if data and isinstance(data, list):
                title = data[0].get("question", "")
                slug = data[0].get("slug", "")
                if "btc" in title.lower() or "bitcoin" in title.lower() or "btc" in slug.lower():
                    matches.append({"id": sid, "title": title})
                    print(f"  [MATCH] ID {sid}: {title}")
        
        if not matches:
            print("No BTC 5m series found via standard series search.")
            # Fallback: search by keywords directly
            print("Attempting Keyword Search...")
            r_search = requests.get("https://gamma-api.polymarket.com/markets", params={"search": "BTC Up or Down", "active": "true"})
            for m in r_search.json():
                print(f"  [KEYWORD MATCH] {m.get('slug')} (Series ID: {m.get('seriesId')})")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    crack_id()
