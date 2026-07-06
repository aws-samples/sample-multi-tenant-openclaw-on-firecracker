#!/bin/bash
# EC2-2 (Wazuh Agent / monitored endpoint) userdata.
# Installs wazuh-agent 4.7.5 pinned to the manager's PRIVATE IP, installs auditd
# (required for whodata = "who changed the file"), and enables real-time FIM +
# whodata on /etc.
#
# Why the manager's PRIVATE IP and not an Elastic IP: the reference article hit
# "agent shows Unknown after restart" because a public IP changes on stop/start,
# and fixed it with an Elastic IP. Here both EC2 live in the same VPC and talk
# over the private 172.31/16 address, which never changes and never leaves the
# VPC — so no public exposure and no EIP needed.
#
# __MANAGER_PRIVATE_IP__ is substituted by setup-wazuh-two-ec2.sh at launch time
# from the running manager instance. It is a placeholder in the repo on purpose.
#
# Verified on a demo account / region, 2026-06-30: agent registered as
# ID 001 Agent-one Active; whodata engine started; touch/echo/rm /etc/fim_test
# produced rule 554/550/553 alerts on the manager within the same second, with
# who=root attached.
set -x
exec > /var/log/agent-bootstrap.log 2>&1
export DEBIAN_FRONTEND=noninteractive

MANAGER_IP="__MANAGER_PRIVATE_IP__"

apt-get update -y
apt-get install -y curl wget auditd
systemctl enable --now auditd

# Wazuh APT repo (4.x)
curl -s https://packages.wazuh.com/key/GPG-KEY-WAZUH | \
  gpg --no-default-keyring --keyring gnupg-ring:/usr/share/keyrings/wazuh.gpg --import
chmod 644 /usr/share/keyrings/wazuh.gpg
echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" \
  > /etc/apt/sources.list.d/wazuh.list
apt-get update -y

# Install agent pinned to manager IP; WAZUH_MANAGER drives auto-enrollment over 1515
WAZUH_MANAGER="$MANAGER_IP" WAZUH_AGENT_NAME="Agent-one" \
  apt-get install -y wazuh-agent=4.7.5-1
systemctl daemon-reload
systemctl enable wazuh-agent
systemctl start wazuh-agent

# --- Real-time FIM + whodata on /etc (the article's core) ---
python3 - <<'PY'
import re
p = "/var/ossec/etc/ossec.conf"
s = open(p).read()
new_sc = """  <syscheck>
    <disabled>no</disabled>
    <!-- lab: scan every 60s; realtime+whodata for instant who-did-it FIM -->
    <frequency>60</frequency>
    <scan_on_start>yes</scan_on_start>
    <alert_new_files>yes</alert_new_files>
    <auto_ignore frequency="10" timeframe="3600">no</auto_ignore>

    <!-- Real-time FIM on /etc WITH whodata (user/process via auditd) -->
    <directories realtime="yes" whodata="yes" report_changes="yes" check_all="yes">/etc</directories>
    <!-- Real-time on system binaries (no whodata needed) -->
    <directories realtime="yes" check_all="yes">/usr/bin,/usr/sbin</directories>
    <directories check_all="yes">/bin,/sbin,/boot</directories>

    <ignore>/etc/mtab</ignore>
    <ignore>/etc/hosts.deny</ignore>
    <ignore>/etc/mail/statistics</ignore>
    <ignore>/etc/random-seed</ignore>
    <ignore>/etc/random.seed</ignore>
    <ignore>/etc/adjtime</ignore>
    <ignore>/etc/httpd/logs</ignore>
    <ignore>/etc/utmpx</ignore>
    <ignore>/etc/wtmpx</ignore>
    <ignore>/etc/cups/certs</ignore>
    <ignore>/etc/dumpdates</ignore>
    <ignore>/etc/svc/volatile</ignore>
    <ignore>/var/log</ignore>
    <ignore type="sregex">.log$|.swp$</ignore>
    <nodiff>/etc/ssl/private.key</nodiff>
    <skip_nfs>yes</skip_nfs>
    <skip_dev>yes</skip_dev>
    <skip_proc>yes</skip_proc>
    <skip_sys>yes</skip_sys>"""
s2 = re.sub(r"  <syscheck>.*?<skip_sys>yes</skip_sys>", new_sc, s, count=1, flags=re.S)
assert s2 != s, "syscheck block not replaced — agent ossec.conf layout changed"
open(p, "w").write(s2)
print("syscheck realtime+whodata applied")
PY

systemctl restart wazuh-agent
sleep 5
echo "AGENT_INSTALL_DONE rc=$?" > /var/log/agent-install-done.marker
