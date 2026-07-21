"""意图模块共享常量与校验(单一来源)。

rules.py / semantic.py 原先各自重复定义 VALID_LEVEL1/2/INDUSTRIES,
抽到此处避免漂移(§五-4)。
"""

from __future__ import annotations

# 有效的意图值域(单一来源)
VALID_LEVEL1 = frozenset({"learn", "code", "build", "doc", "translate", "unsupported"})
VALID_LEVEL2 = frozenset({
    "explain", "debug", "compare", "casual",
    "snippet", "component", "fix", "refactor",
    "page", "site", "modify", "game",
    "readme", "tutorial", "plan",
    "text", "code_lang", "design", "search",
})
VALID_INDUSTRIES = frozenset({
    "restaurant", "ecommerce", "gov", "edu", "health",
    "finance", "game", "personal", "corp", "tech", "media",
    "travel", "other", "none",
})

# 旧版意图名 → 新版 (level1, level2)
OLD_TO_LEVELS = {
    "build_site": ("build", "site"),
    "build_page": ("build", "page"),
    "code_snippet": ("code", "snippet"),
    "learn_explain": ("learn", "explain"),
    "learn_casual": ("learn", "casual"),
}


def is_valid_level1(v: str) -> bool:
    return v in VALID_LEVEL1


def is_valid_level2(v: str) -> bool:
    return v in VALID_LEVEL2


def is_valid_industry(v: str) -> bool:
    return v in VALID_INDUSTRIES


def normalize_industry(v: str | None) -> str:
    """行业字段归一化: 非法值 → other。"""
    if not v or v not in VALID_INDUSTRIES:
        return "other"
    return v


# ── 安全层共享关键词(单一来源, run_safety 引用; 防与 INTENT_SYSTEM 漂移, Tier 3) ──
# 注意:
#  - 中文关键词大小写无关(匹配前未 lower, 但中文无大小写);
#  - 英文关键词一律小写(匹配前已对输入 lower());
#  - 尽量用「多字短语/英文单词」而非单字, 降低误拦(例如不单独放"删除",
#    而是"清空数据库"这类明确危险短语); 既有单字(删除/支付)为历史行为, 暂保留。
SAFETY_CRITICAL_KEYWORDS = frozenset({
    # ── 破坏性 / 数据丢失 ──
    "删除", "清空", "drop", "rm ", "remove", "del ", "delete",
    "清空数据库", "清库", "drop table", "delete from",
    "格式化", "format", "mkfs", "fdisk", "truncate",
    "重置所有", "恢复出厂", "rm -rf", "sudo rm", "shutdown", "关机", "kill -9", "pkill",
    # ── 凭证 / 身份 / 资金(外泄或滥用) ──
    "支付", "付款", "充值", "订单", "交易", "转账",
    "密码", "密钥", "token", "api_key", "secret",
    "银行卡", "信用卡", "credit card", "身份证", "ssn", "私钥", "凭证", "credential",
    # ── 注入 / 越权 / 滥用 ──
    "sql注入", "注入攻击", "xss", "越权", "提权", "exploit", "pwn",
    "绕过验证", "bypass", "爬取数据", "抓取数据", "爬虫",
})

SAFETY_HIGH_KEYWORDS = frozenset({
    # ── 发布 / 部署(既有) ──
    "发布", "上线", "deploy", "publish",
    "管理", "admin", "后台",
    "修改权限", "更改角色",
    # ── 数据迁移 / 配置变更(新增) ──
    "导出", "export", "备份", "backup", "迁移", "migrate",
    "升级", "upgrade", "重启", "restart", "停止服务", "stop",
    "提交", "commit", "合并", "merge",
    "封禁", "ban", "删除用户", "移除用户", "改密码", "重置密码",
    "grant", "revoke", "权限", "用户管理",
})

SAFETY_MEDIUM_KEYWORDS = frozenset({
    "修改", "改", "modify", "update", "更新",
    "新增", "添加", "add", "create",
    # ── 编辑 / 配置类(新增) ──
    "调整", "微调", "编辑", "edit", "配置", "config",
    "重命名", "rename", "设置", "改色", "换色", "排版", "布局调整",
})

# INTENT_SYSTEM 中"unsupported"判据(分类器与规则层共享同一句描述, Tier 3)
UNSUPPORTED_HINT = (
    "(注意: 仅处理「网页前端」需求; 以下一律 → unsupported: "
    "后端开发/服务端/数据库/API服务/App/小程序原生/游戏引擎/桌面应用/嵌入式/"
    "运维部署/DevOps/爬虫/自动化脚本/微信生态开发/数据抓取/AI模型训练 等非网页前端需求)"
)


def build_skill_sys(base_sys: str, project_system_prompt: str | None) -> str:
    """把项目系统 prompt 作为「项目约束」段追加到 skill 的 system prompt(Tier 1)。

    空值/None 时原样返回, 向后兼容(老项目无 system_prompt 行为不变)。
    """
    if not project_system_prompt:
        return base_sys
    return base_sys + "\n\n# 项目约束(用户定制, 必须遵循)\n" + project_system_prompt


def parse_project_constraints(system_prompt: str | None) -> list[str]:
    """从项目 system_prompt 抽取结构化禁用意图/词(Tier 2)。

    约定: system_prompt 内以独立行 `--forbid: deploy, payment` 声明,
    逗号或空白分隔。只取结构化片段, 绝不解析自由文本(防误拦/漏拦)。
    无声明返回空列表。
    """
    if not system_prompt:
        return []
    out: list[str] = []
    for line in system_prompt.splitlines():
        s = line.strip()
        if not s.lower().startswith("--forbid:"):
            continue
        body = s[len("--forbid:"):].strip()
        for tok in body.replace(",", " ").split():
            tok = tok.strip().strip("\"'")
            if tok:
                out.append(tok.lower())
    return out
