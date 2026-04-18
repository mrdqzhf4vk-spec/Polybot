import paramiko
import sys

def run_diagnostics():
    host = '62.171.172.77'
    user = 'root'
    pwd = 'ItAyCo15$'

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=pwd, timeout=10)
        
        commands = [
            "echo '--- DISK ---' && df -h",
            "echo '--- MEMORY ---' && free -m",
            "echo '--- PROCESSES ---' && ps aux | grep -E 'polybot|python3' | grep -v grep",
            "echo '--- RECENT LOGS ---' && tail -n 100 /tmp/screen_log",
            "echo '--- FILE TIMES ---' && ls -lt /root/polybot/portfolio_*.json | head -n 10",
            "echo '--- SCREEN LIST ---' && screen -list"
        ]
        
        for cmd in commands:
            print(f"Executing: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            print(stdout.read().decode('utf-8', 'ignore'))
            err = stderr.read().decode('utf-8', 'ignore')
            if err:
                print(f"Error: {err}")
            print("-" * 40)
            
        ssh.close()
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    run_diagnostics()
