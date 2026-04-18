import paramiko, json, requests

host = '62.171.172.77'
user = 'root'
pwd = 'ItAyCo15$'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pwd, timeout=10)

stdin, stdout, stderr = ssh.exec_command('cat /root/polybot/portfolio_0xdeca32.json')
data = json.loads(stdout.read().decode())

print(f"Balance: ${data['virtual_usdc']:.2f}")
print(f"Realized PnL: ${data['realized_pnl']:.2f}")
print(f"Trades: {data['total_trades_taken']}")
print(f"Open positions: {len(data['open_positions'])}")

print("\n--- OPEN POSITIONS ---")
for pos in data['open_positions']:
    print(f"  Market: {pos['market_id'][:20]}")
    print(f"  Invested: ${pos['amount_usdc_invested']:.4f} | Tokens: {pos['amount_tokens']:.4f} | Entry price: {pos['price_bought']:.4f}")
    print()

# Now fetch real trade history to see what sell transactions look like
print("\n--- CHECKING LAST 10 SELL EVENTS FROM API ---")
wallet = '0xdeca32a63c3f45b40d12e9c7b38e93f00a09c81d'
# Find full wallet address from portfolio filename - its 0xdeca32
# Let's get the actual full address from the VPS screen log
stdin, stdout, stderr = ssh.exec_command('screen -S polybot -X hardcopy /tmp/log.txt && sleep 1 && grep -i "0xdeca32" /tmp/log.txt | head -5')
log = stdout.read().decode('utf-8', errors='replace')
print("Log mentions:", log[:500])

ssh.close()
