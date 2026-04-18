import requests
import json
import time

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Compute current slug
now = int(time.time())
block_start = (now // 300) * 300
slug = f"btc-updown-5m-{block_start}"

print(f"DIAGNOSTIC FOR SLUG: {slug}")

# 1. Test Gamma Markets
print("\n--- GAMMA /markets ---")
try:
    r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug})
    print(f"Status: {r.status_code}")
    data = r.json()
    if data:
        m = data[0]
        print(f"Title: {m.get('question')}")
        print(f"Outcome Prices: {m.get('outcomePrices')}")
        print(f"CLOB IDs: {m.get('clobTokenIds')}")
        print(f"Last Trade Price: {m.get('lastTradePrice')}")
        print(f"Best Bid/Ask: {m.get('bestBid')} / {m.get('bestAsk')}")
    else:
        print("No market found for this slug.")
except Exception as e:
    print(f"Error: {e}")

# 2. Test CLOB Book for the IDs
if data:
    tokens = json.loads(m.get('clobTokenIds', '[]'))
    if tokens:
        print(f"\nUP Token ID: {tokens[0]}")
        print("\n--- CLOB /book (UP) ---")
        try:
            # Try both ticker and token_id
            r = requests.get(f"{CLOB_API}/book", params={"token_id": tokens[0]})
            print(f"Status: {r.status_code}")
            print(f"Book: {json.dumps(r.json(), indent=2)[:500]}")
        except Exception as e:
            print(f"Error: {e}")
