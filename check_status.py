import paramiko

host = '62.171.172.77'
user = 'root'
pwd = 'ItAyCo15$'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pwd, timeout=10)

# Check for the actual python process, specifically looking for the script name
stdin, stdout, stderr = ssh.exec_command('ps aux | grep "python3 polybot.py" | grep -v grep | grep -v SCREEN')
proc = stdout.read().decode().strip()

# Check log file activity
stdin, stdout, stderr = ssh.exec_command('ls -l /root/polybot/bot.log')
log_file = stdout.read().decode().strip()
stdin, stdout, stderr = ssh.exec_command('tail -n 5 /root/polybot/bot.log')
last_logs = stdout.read().decode().strip()

stdin, stdout, stderr = ssh.exec_command('screen -list')
screens = stdout.read().decode().strip()

print("=== PROCESS STATUS ===")
if proc:
    lines = proc.split('\n')
    for line in lines:
        parts = line.split()
        pid = parts[1]
        cpu = parts[2]
        mem = parts[3]
        uptime = parts[9]
        print(f"[RUNNING] PID: {pid} | CPU: {cpu}% | MEM: {mem}% | Started: {uptime}")
else:
    print("[NOT RUNNING] Polybot python process is not active!")

print("\n=== LOG FILE STATUS ===")
if log_file:
    print(f"Log found: {log_file}")
    print("Last 5 lines:")
    print("-" * 30)
    print(last_logs if last_logs else "(Empty log)")
    print("-" * 30)
else:
    print("[MISSING] bot.log not found on VPS.")

print("\n=== SCREEN SESSIONS ===")
print(screens if screens else "No screen sessions found.")

ssh.close()
