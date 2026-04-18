import paramiko, time

host = '62.171.172.77'
user = 'root'
pwd = 'ItAyCo15$'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pwd, timeout=10)

# Stop the bot
print("[1] Stopping polybot screen...")
ssh.exec_command('screen -X -S polybot quit')
time.sleep(2)

# Delete all portfolio files
print("[2] Deleting all portfolio files...")
stdin, stdout, stderr = ssh.exec_command('rm -f /root/polybot/portfolio_*.json')
stdout.channel.recv_exit_status()
print("    Done.")

# Verify clean
stdin, stdout, stderr = ssh.exec_command('ls /root/polybot/portfolio_*.json 2>/dev/null || echo CLEAN')
result = stdout.read().decode().strip()
print(f"    Remaining files: {result}")

# Restart bot fresh
print("[3] Restarting polybot with clean slate...")
ssh.exec_command('cd /root/polybot && screen -S polybot -d -m python3 polybot.py --auto')
time.sleep(2)

# Confirm running
stdin, stdout, stderr = ssh.exec_command('ps aux | grep "polybot.py --auto" | grep -v grep')
proc = stdout.read().decode().strip()
if proc:
    print("[OK] Polybot is running fresh!")
    print(f"    PID: {proc.split()[1]}")
else:
    print("[ERROR] Could not confirm polybot is running!")

ssh.close()
print("\nDone. Portfolios have been reset. 24h clean run starts NOW.")
