#!/usr/bin/env python
# 在服务器后台跑一次真实单页建站(超管账号), 走完整生成 + 后置 QC 单裁判, 让线上 QcScore 有数据。
# 之后轮询 /admin/quality 直到 qc_count>0, 打印最终质量摘要。含密码, 不提交。
import paramiko, time, json
HOST="1.12.219.195"; PORT=22; USER="root"; PW="Huzhen189"
SEED = "帮我生成一个简单的咖啡馆介绍单页网站，包含店名「晨光咖啡」、一段品牌简介、三款招牌饮品菜单，以及营业时间和联系方式。浅色清新风格。"
PY="/opt/miniconda3/envs/seedai/bin/python"

ssh=paramiko.SSHClient(); ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST,port=PORT,username=USER,password=PW,timeout=20,look_for_keys=False,allow_agent=False)

# 1) 起后台生成(setsid 脱离 SSH 会话, 独立运行)
launch = (
    f"setsid bash -c 'cd /home/huzhen/seedai/backend && "
    f"SEEDAI_BASE=http://127.0.0.1:7101 SEEDAI_SIM_MODEL=qwen {PY} _test_multipage.py "
    f"--seed {json.dumps(SEED)} > /tmp/mp.log 2>&1' &"
)
ssh.exec_command(launch)  # 不等待
print("[launch] 后台生成已启动 (multipage harness, model=qwen)")

# 2) 轮询 qc_count 直到 >0 或超时(最多 ~15 分钟)
probe = (
    "COOKIE=$(mktemp); "
    "curl -s -c $COOKIE -X POST https://seedai.huzhen.net.cn/auth/login -H 'Content-Type: application/json' "
    "-d '{\"account\":\"huzhen\",\"password\":\"huzhen189\"}' -k -o /dev/null; "
    "curl -s -b $COOKIE https://seedai.huzhen.net.cn/admin/quality -k"
)
deadline = time.time() + 900
while time.time() < deadline:
    i,o,e = ssh.exec_command(probe, timeout=30)
    try:
        d = json.loads(o.read().decode(errors="replace"))
        qc = d.get("qc_count", 0)
        fb = d.get("feedback_count", 0)
        gen = d.get("generation_total", 0)
        print(f"[poll] qc_count={qc} feedback_count={fb} generation_total={gen}")
        if qc and qc > 0:
            print("[done] QC 已产生")
            break
    except Exception as ex:
        print("[poll] parse err", ex)
    time.sleep(20)

# 3) 打印最终质量摘要(供用户复查)
i,o,e = ssh.exec_command(probe, timeout=30)
d = json.loads(o.read().decode(errors="replace"))
print("=== FINAL /admin/quality ===")
print("feedback_count:", d.get("feedback_count"))
print("qc_count:", d.get("qc_count"))
print("qc_overall_avg:", d.get("qc_overall_avg"))
print("qc_dimensions:", d.get("qc_dimensions"))
print("qc_judges:", d.get("qc_judges"))
print("qc_overall_dim_avg:", d.get("qc_overall_dim_avg"))
ssh.close()
print(">>> SEED_QC_DONE")
