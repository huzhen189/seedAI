#!/usr/bin/env python
# 启用 seedai.huzhen.net.cn 的 HTTPS: 校验证书存在 -> uncomment 443 段 + 开 80->443 跳转 -> reload。
# 证书必须先在 /root/seedai.huzhen.net.cn/ 就位(Nginx 版: seedai.huzhen.net.cn_bundle.crt + .key)。
import paramiko, re, time
HOST="1.12.219.195"; PORT=22; USER="root"; PW="Huzhen189"
CONF="/etc/nginx/conf.d/seedai.huzhen.net.cn.conf"
CERT_DIR="/root/seedai.huzhen.net.cn"
CERT=f"{CERT_DIR}/seedai.huzhen.net.cn_bundle.crt"
KEY=f"{CERT_DIR}/seedai.huzhen.net.cn.key"

ssh = paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST,port=PORT,username=USER,password=PW,timeout=15,look_for_keys=False,allow_agent=False)

# 1) 校验证书存在
chk = f"test -f {CERT} && test -f {KEY} && echo BOTH_OK || echo MISSING"
r = ssh.exec_command(chk, timeout=20)[1].read().decode().strip()
print("cert check:", r)
if r != "BOTH_OK":
    print(f">>> 证书未就位, 中止。请先从腾讯云下载 Nginx 版证书传到 {CERT_DIR}"); ssh.close(); raise SystemExit(1)

# 2) 读取 conf, 取消 443 段注释 + 打开 80->443 跳转
sftp = ssh.open_sftp()
data = sftp.open(CONF).read().decode()
# 取消整段 443 server 的注释: 把行首 "# server {" / "#     xxx" 还原
lines = data.split("\n")
out = []
in_https = False
for ln in lines:
    s = ln
    if s.strip().startswith("# server {") and "listen 443" not in s:
        # 检测是否 443 段: 往下几行找 listen 443
        pass
    # 通用: 去掉注释掉的 443 段内容行(行首 "#     " 或 "# server {")
    if ln.startswith("# server {") or ln.startswith("#     ") or ln.startswith("#         ") or ln.startswith("#         ") :
        # 仅当处于 HTTPS 注释块才解注释; 用简单策略: 解注释所有以 "# server {" 开头和以 "#     " 开头的行
        if ln.startswith("# server {"):
            out.append(ln[2:])
            in_https = True
        elif in_https and ln.startswith("#"):
            out.append(ln[2:])
        else:
            out.append(ln)
    else:
        if ln.strip() == "# }" and in_https:
            out.append("}")
            in_https = False
        else:
            out.append(ln)
data = "\n".join(out)
# 打开 80->443 跳转
data = data.replace("#     return 301 https://$server_name$request_uri;", "    return 301 https://$server_name$request_uri;")
sftp.open(CONF, "w").write(data.encode()); sftp.close()

# 3) 校验 + reload
test = ssh.exec_command("nginx -t", timeout=30)
out_t=test[1].read().decode(); err_t=test[2].read().decode()
print("nginx -t:", out_t.strip(), err_t.strip())
if "test is successful" not in out_t+err_t:
    print(">>> nginx 语法错误, 未 reload, 主域不受影响"); ssh.close(); raise SystemExit(1)
ssh.exec_command("systemctl reload nginx", timeout=30)
print(">>> reloaded")
time.sleep(2)
v = ssh.exec_command("curl -s -m5 -k -o /dev/null -w 'https443:%{http_code}\\n' https://127.0.0.1:443/ -H 'Host: seedai.huzhen.net.cn'", timeout=20)[1].read().decode().strip()
print("verify 443:", v)
ssh.close()
print(">>> HTTPS_ENABLED")
