#!/bin/bash
# One-time setup on the iMariners Webyne VPS (run as root): lets Claude's key log in as "claudeops".
# Usage: curl -sL https://cdn.jsdelivr.net/gh/iMariner/imariners-sire/vps.sh | bash
set -e
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDntcvVXvBeMMSNGxD7dh67LZXV9Bbiac/LCCAU9MlCJ claudeops@imariners-vps'
id claudeops >/dev/null 2>&1 || adduser --disabled-password --gecos "" claudeops
install -d -m 700 -o claudeops -g claudeops /home/claudeops/.ssh
touch /home/claudeops/.ssh/authorized_keys
grep -qF "$KEY" /home/claudeops/.ssh/authorized_keys || echo "$KEY" >> /home/claudeops/.ssh/authorized_keys
chown claudeops:claudeops /home/claudeops/.ssh/authorized_keys
chmod 600 /home/claudeops/.ssh/authorized_keys
loginctl enable-linger claudeops 2>/dev/null || true
command -v fail2ban-client >/dev/null && fail2ban-client unban --all >/dev/null 2>&1 || true
ALLOW=$(sshd -T 2>/dev/null | grep -i '^allowusers' || true)
if [ -n "$ALLOW" ] && ! echo "$ALLOW" | grep -qw claudeops; then
  echo "AllowUsers $(echo "$ALLOW" | cut -d' ' -f2-) claudeops" > /etc/ssh/sshd_config.d/98-claudeops.conf
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
fi
echo
echo "READY: claudeops can log in now. Tell Claude 'done'."
