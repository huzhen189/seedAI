"""v1.0 重构后独立校验：import app.skills 无 ImportError + 意图路由正确。

仅依赖标准库 + subprocess；不修改任何文件。
"""
from __future__ import annotations
import subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AI = REPO / "backend" / "ai_service"
PY = r"C:\Users\zhenhu\.workbuddy\binaries\python\envs\seedai-biz\Scripts\python.exe"

import_test = (
    "import sys\n"
    "sys.path.insert(0, r'%s')\n"
    "import app.skills as s\n"
    "print('IMPORT_OK agents=' + str(len(s.__all__)) + ' ' + str(s.__all__))\n"
) % str(AI)

route_test = (
    "import sys\n"
    "sys.path.insert(0, r'%s')\n"
    "from app.intent.tools import INTENT_SKILL_MAP\n"
    "probes = [('chat','explain'),('chat','search'),('chat','design'),('chat','translate'),\n"
    "          ('build','requirement'),('build','site'),('build','page'),('build','review'),('build','fix'),('build','doc')]\n"
    "res = {a + '/' + b: INTENT_SKILL_MAP.get((a,b)) for a,b in probes}\n"
    "print('ROUTE_OK ' + str(res))\n"
) % str(AI)

# py_compile 全部 skills
bad = []
for py in (AI / "app" / "skills").rglob("*.py"):
    r = subprocess.run([PY, "-m", "py_compile", str(py)], capture_output=True, text=True)
    if r.returncode != 0:
        bad.append((str(py.relative_to(REPO)), r.stderr))
print("py_compile:", "OK" if not bad else f"FAIL {bad}")

r1 = subprocess.run([PY, "-c", import_test], capture_output=True, text=True)
print("import app.skills:", (r1.stdout or r1.stderr).strip().splitlines()[0] if (r1.stdout or r1.stderr).strip() else "NO OUTPUT")

r2 = subprocess.run([PY, "-c", route_test], capture_output=True, text=True)
print("routing:", (r2.stdout or r2.stderr).strip().splitlines()[0] if (r2.stdout or r2.stderr).strip() else "NO OUTPUT")
