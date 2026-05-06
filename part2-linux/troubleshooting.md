# Part 2A: Linux Troubleshooting — Unresponsive Server (10.0.1.50)

## Mental Model First

Before running commands, think about what could cause SSH timeout:
1. **Network** — firewall rule changed, security group blocked, routing issue
2. **SSH daemon** — service crashed, config corrupted, port changed
3. **System resource exhaustion** — OOM killed sshd, disk full, CPU 100%
4. **Host-level firewall** — iptables/ufw rule blocking port 22
5. **Key/auth issue** — this would give a rejection, not a timeout, but worth checking

Work from the outside in: network → service → system.

---

## Step 1: Verify Network Connectivity from Jump Host

```bash
# First, can we reach the host at all?
ping -c 4 10.0.1.50

# Check if port 22 is reachable (3 second timeout)
nc -zv -w 3 10.0.1.50 22

# If nc isn't available:
timeout 3 bash -c 'cat < /dev/null > /dev/tcp/10.0.1.50/22' && echo "Port open" || echo "Port closed/filtered"

# Trace the route to identify where packets are dropping
traceroute 10.0.1.50

# Check AWS Security Groups / NACLs from the AWS CLI
aws ec2 describe-security-groups --filters "Name=ip-permission.from-port,Values=22"
aws ec2 describe-network-acls --filters "Name=vpc-id,Values=vpc-XXXXX"
```

**What to look for:**
- `ping` responds but SSH times out → SSH service issue or host-level firewall
- `ping` doesn't respond → Network/NACL/Security Group issue
- `traceroute` stops before reaching host → Routing or NACL issue

---

## Step 2: Check if SSH Service is Running (via AWS Systems Manager)

Since we can't SSH in, use SSM Session Manager (this is exactly why you should always have SSM configured):

```bash
# Start SSM session (no SSH needed)
aws ssm start-session --target i-0abc123def456789

# Once inside, check SSH status
sudo systemctl status sshd
sudo systemctl status ssh  # Ubuntu uses 'ssh' not 'sshd'

# Check if it's listening on the expected port
sudo ss -tlnp | grep sshd
sudo netstat -tlnp | grep :22

# Check sshd config for any port changes
sudo grep -E "^Port|^ListenAddress" /etc/ssh/sshd_config

# Check if the process is actually running
ps aux | grep sshd
```

**If SSH is stopped:**
```bash
sudo systemctl start sshd
sudo systemctl enable sshd  # Make it start on reboot
```

---

## Step 3: SSH Running But Still Can't Connect — What Else?

```bash
# 1. Check host-level firewall (iptables)
sudo iptables -L INPUT -n -v | grep -E "22|DROP|REJECT"
sudo ufw status  # Ubuntu UFW

# 2. Check if authorized_keys is intact
cat ~/.ssh/authorized_keys
ls -la ~/.ssh/  # Permissions must be 700 for .ssh, 600 for authorized_keys

# 3. Check SSH daemon logs for rejection reasons
sudo journalctl -u sshd --since "1 hour ago" --no-pager
sudo tail -100 /var/log/auth.log  # Ubuntu
sudo tail -100 /var/log/secure    # RHEL/Amazon Linux

# 4. Check if fail2ban or similar is blocking your IP
sudo fail2ban-client status sshd
sudo fail2ban-client set sshd unbanip YOUR_JUMP_HOST_IP

# 5. Check if /etc/hosts.deny is blocking you
cat /etc/hosts.deny
cat /etc/hosts.allow

# 6. Verify the host key hasn't changed (causes "REMOTE HOST IDENTIFICATION HAS CHANGED")
ssh-keyscan 10.0.1.50
```

---

## Step 4: Check CPU, Memory, and Disk Usage

```bash
# === CPU ===
# Current load average (1min, 5min, 15min)
uptime
# Or: cat /proc/loadavg

# Detailed CPU usage - top offenders
top -b -n 1 | head -30
# Better: show only high-CPU processes
ps aux --sort=-%cpu | head -20

# Check if it's a single-core saturation issue
mpstat -P ALL 1 3

# === MEMORY ===
free -h
# Look for: available memory near 0 = memory pressure
# Look for: swap usage = system is swapping (performance killer)

# What's eating memory?
ps aux --sort=-%mem | head -20

# Check for OOM killer activity
sudo dmesg | grep -i "oom\|killed process" | tail -20
sudo journalctl -k | grep -i oom | tail -20

# === DISK ===
# Overall disk usage
df -hT

# Find what's eating space (start from root, find directories > 1GB)
sudo du -sh /* 2>/dev/null | sort -rh | head -20

# Check if disk is full causing write failures
df -h | awk '$5 >= 90 {print "WARNING: "$6" is "$5" full"}'

# Check inode usage (files, not space — often overlooked)
df -i

# Check disk I/O — is it thrashing?
iostat -x 1 3
# or
iotop -b -n 3 | head -20
```

---

## Step 5: Check Recent System Logs for Errors

```bash
# === Kernel messages (hardware errors, OOM, panics) ===
sudo dmesg -T | tail -50
sudo dmesg -T | grep -E "error|fail|warn|oom|panic" -i | tail -30

# === System journal (systemd) — most important ===
# Last 100 lines of everything
sudo journalctl -n 100 --no-pager

# Last hour, priority: error or higher
sudo journalctl --since "1 hour ago" -p err --no-pager

# Specific service failures
sudo journalctl -u nginx --since "2 hours ago" --no-pager
sudo journalctl -u mysql --since "2 hours ago" --no-pager

# === Application logs ===
sudo tail -100 /var/log/syslog       # Ubuntu general
sudo tail -100 /var/log/messages     # RHEL/Amazon Linux
sudo tail -100 /var/log/auth.log     # Authentication events
sudo tail -100 /var/log/nginx/error.log   # If nginx is running
sudo tail -100 /var/log/nginx/access.log

# === Check for recent reboots or crashes ===
last reboot | head -10
who -b  # Last boot time
sudo journalctl --list-boots  # Boot history

# === Check crontab for anything that might have run ===
sudo crontab -l
cat /var/log/cron  # Amazon Linux
sudo journalctl -u cron --since "2 hours ago"  # Ubuntu
```

---

## Quick Diagnosis Checklist

| Check | Command | Red Flag |
|-------|---------|----------|
| Network reachable | `ping 10.0.1.50` | No response |
| Port 22 open | `nc -zv 10.0.1.50 22` | Connection refused/timeout |
| SSH service | `systemctl status sshd` | inactive/failed |
| CPU load | `uptime` | Load > 4x CPU count |
| Memory | `free -h` | Available < 100MB |
| Disk full | `df -h` | Any mount > 95% |
| OOM events | `dmesg \| grep oom` | Any results |
| Recent errors | `journalctl -p err -n 50` | Error count |

## Resolution Steps by Root Cause

| Root Cause | Fix |
|------------|-----|
| Security Group blocked port 22 | Add SSH rule in AWS console |
| sshd service crashed | `systemctl restart sshd` via SSM |
| Disk full | `find / -name "*.log" -size +100M` then rotate/delete |
| OOM killed sshd | Restart sshd, then address memory leak |
| fail2ban banned IP | `fail2ban-client set sshd unbanip <IP>` |
| iptables blocked | `iptables -D INPUT -p tcp --dport 22 -j DROP` |
