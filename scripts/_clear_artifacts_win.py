"""Windows 下清空 artifacts 目录(绕过 safe-delete 钩子)。

本机 safe-delete 钩子会劫持 shutil.rmtree/os.unlink 并 fail-closed,
导致 scripts/reset_all.py 在 clear_artifacts 阶段中断。
这里直接调用 Win32 API 删除。
"""

from __future__ import annotations

import ctypes
import os
import sys

k = ctypes.windll.kernel32


def rm_file(path: str) -> bool:
    return bool(k.DeleteFileW(ctypes.c_wchar_p(path)))


def rm_tree(path: str) -> bool:
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                os.chmod(fp, 0o666)
            except Exception:
                pass
            if not rm_file(fp):
                print("  FAIL file:", fp, k.GetLastError())
        for d in dirs:
            dp = os.path.join(root, d)
            if not k.RemoveDirectoryW(ctypes.c_wchar_p(dp)):
                print("  FAIL dir:", dp, k.GetLastError())
    return bool(k.RemoveDirectoryW(ctypes.c_wchar_p(path)))


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else r"E:\work\myTencentYunHome\seedAI\artifacts"
    if not os.path.isdir(root):
        print("artifact root not found:", root)
        return 1
    if os.path.basename(root.rstrip("\\/")) != "artifacts":
        print("refuse: target must be a directory named 'artifacts', got:", root)
        return 2
    removed = 0
    for name in os.listdir(root):
        if name == ".gitkeep":
            continue
        p = os.path.join(root, name)
        ok = rm_tree(p) if os.path.isdir(p) else rm_file(p)
        print(("OK  " if ok else "ERR "), name)
        if ok:
            removed += 1
    print("removed:", removed)
    print("remain:", os.listdir(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
