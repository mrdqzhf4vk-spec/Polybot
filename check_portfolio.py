import paramiko, json, time

host = '62.171.172.77'
user = 'root'
pwd = 'ItAyCo15$'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pwd, timeout=10)

stdin, stdout, stderr = ssh.exec_command('ls /root/polybot/portfolio_*.json')
files = stdout.read().decode().strip().split()

print("=" * 60)
print("  PORTFOLIO SANITY CHECK")
print("=" * 60)

for f in files:
    stdin, stdout, stderr = ssh.exec_command('cat ' + f)
    raw = stdout.read().decode()
    data = json.loads(raw)
    wallet = f.split('portfolio_')[1].replace('.json','')
    
    balance = data["virtual_usdc"]
    realized = data["realized_pnl"]
    trades = data["total_trades_taken"]
    open_pos = len(data["open_positions"])
    
    print(f"\nWallet: {wallet}")
    print(f"  Balance:       ${balance:.2f}")
    print(f"  Realized PnL:  ${realized:.2f}")
    print(f"  Trades taken:  {trades}")
    print(f"  Open positions: {open_pos}")

    # Max possible PnL = trades * max_per_trade($5) * 20x (polymarket max odds)
    # But realistically at $5 max per trade, a 100% winner = $5 profit
    # So if realized > trades * 5, something is inflated
    max_realistic = trades * 5.0
    
    if trades == 0:
        print("  [WARNING] No trades taken yet.")
    elif realized > max_realistic:
        print(f"  [INFLATED] Max realistic PnL = ${max_realistic:.0f}. This looks like a bug artifact.")
    elif realized < 0:
        print(f"  [LOSS] Net negative, sweeper may have wiped too many positions.")
    else:
        roi = (realized / (trades * 2.5)) * 100  # assuming avg $2.5 invested per trade
        print(f"  [OK] Looks realistic. Approx ROI per trade: {roi:.0f}%")

ssh.close()
