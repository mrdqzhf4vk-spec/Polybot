import paramiko, time

host = '62.171.172.77'
user = 'root'
pwd = 'ItAyCo15$'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(host, username=user, password=pwd, timeout=10)

sftp = ssh.open_sftp()
sftp.put('polybot.py', '/root/polybot/polybot.py')
sftp.close()
print('File uploaded.')

# Stop existing bot
ssh.exec_command('screen -X -S polybot quit')
ssh.exec_command('pkill -f "python3 polybot.py"')
time.sleep(2)

# Reset log and start
print('Starting bot...')
ssh.exec_command('rm /root/polybot/bot.log')
ssh.exec_command('cd /root/polybot && screen -S polybot -d -m python3 polybot.py --auto')
time.sleep(5)

# Verify
stdin, stdout, stderr = ssh.exec_command('ps aux | grep "python3 polybot.py" | grep -v grep')
proc = stdout.read().decode().strip()
print('Bot Process Found:', bool(proc))
if proc:
    print(proc)

ssh.close()
