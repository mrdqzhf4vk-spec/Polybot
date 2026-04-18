import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import requests, time, json

# Test 1: Binance BTC price
r = requests.get('https://api.binance.com/api/v3/ticker/price', params={'symbol':'BTCUSDT'}, timeout=5)
btc = float(r.json()['price'])
print(f'Binance BTC: ${btc:,.2f}')

# Test 2: Current market slug
now = int(time.time())
block_start = (now // 300) * 300
slug = f'btc-updown-5m-{block_start}'
secs_left = (block_start + 300) - now
print(f'Current slug: {slug}')
print(f'Seconds left: {secs_left}s')

# Test 3: Polymarket event
r2 = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=8)
events = r2.json()
if events:
    ev = events[0]
    print(f'Event title: {ev.get("title", "N/A")}')
    mkts = ev.get('markets', [])
    for mkt in mkts:
        toks = json.loads(mkt.get('clobTokenIds', '[]'))
        outs = json.loads(mkt.get('outcomes', '[]'))
        print(f'  Outcomes: {outs}')
        if toks:
            r3 = requests.get('https://clob.polymarket.com/book', params={'token_id': toks[0]}, timeout=5)
            book = r3.json()
            bids = book.get('bids', [])
            asks = book.get('asks', [])
            bid = float(bids[0]['price']) if bids else 0
            ask = float(asks[0]['price']) if asks else 1
            mid = (bid+ask)/2
            print(f'  Token[0] bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}')
else:
    print('Event not found for current slug - will auto-detect at runtime')

print('Tests done!')
