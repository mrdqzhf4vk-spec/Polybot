import requests

# Test data API
r1 = requests.get("https://data-api.polymarket.com/trades?user=0x37c1874a60d348903594a96703e0507c518fc53a&limit=2")
print("Data API:", r1.status_code, r1.text[:200])

# Test CLOB API
r2 = requests.get("https://clob.polymarket.com/trades?maker_address=0x37c1874a60d348903594a96703e0507c518fc53a&limit=2")
print("CLOB API:", r2.status_code, r2.text[:200])
