"""意图模块共享常量与校验(单一来源)。

rules.py / semantic.py 原先各自重复定义 VALID_LEVEL1/2/INDUSTRIES,
抽到此处避免漂移(§五-4)。
"""

from __future__ import annotations

# 有效的意图值域(v1.0: Chat/Build 两大方向)
VALID_LEVEL1 = frozenset({"chat", "build", "unsupported"})
VALID_LEVEL2 = frozenset({
    # Chat 方向
    "casual", "explain", "compare", "search", "design", "translate",
    # Build 方向
    "requirement", "site", "page", "modify", "fix", "review", "game",
})
VALID_INDUSTRIES = frozenset({
    "restaurant", "ecommerce", "gov", "edu", "health",
    "finance", "game", "personal", "corp", "tech", "media",
    "travel", "other", "none",
})

# 旧版意图名 → 新版(v1.0 Chat/Build)
OLD_TO_LEVELS = {
    "learn_explain": ("chat", "explain"),
    "learn_casual": ("chat", "casual"),
    "learn_search": ("chat", "search"),
    "learn_design": ("chat", "design"),
    "build_site": ("build", "site"),
    "build_page": ("build", "page"),
    "code_fix": ("build", "fix"),
    "code_review": ("build", "review"),
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
# 两类:
#  1) HARD  关键词: 命中即危险, 不受建设性语境中和(破坏性/滥用短语)。始终 critical 拦截。
#  2) SOFT  关键词: 可能出现在正常建站需求(如"支付页面""密码输入框""导出按钮"),
#     需结合「建设性前导 / UI 语境」判断; 命中语境则中和(降级为无风险, 不拦截)。
# 分层(soft_critical/high/medium)对应原 CRITICAL/HIGH/MEDIUM 的严重度。
#
# 英文关键词一律小写(匹配前已对输入 lower()); 中文无大小写。
# 注意: SOFT critical 里的裸词(如"删除")若同时是某 SOFT high 短语(如"删除用户")
# 的子串, 会被 run_safety 重叠降级到 high, 避免误升 critical(v0.8.3)。

# HARD: 真正的破坏性/滥用短语, 几乎只出现在危险语境, 永远拦截
SAFETY_HARD_KEYWORDS = frozenset({
    # ── 破坏性 / 数据丢失(短语级) ──
    "清空数据库", "清库", "删除数据库", "删库", "drop table", "drop database",
    "delete from", "truncate", "truncate table",
    "导出数据库", "export database", "备份数据库", "删除所有", "删除全部",
    "rm -rf", "sudo rm", "kill -9", "pkill", "fdisk", "mkfs",
    "格式化", "format", "恢复出厂", "重置所有",
    # ── 注入 / 越权 / 滥用 ──
    "sql注入", "sql 注入", "注入攻击", "注入漏洞", "xss", "越权", "提权", "exploit", "pwn",
    "拖库", "脱库", "撞库", "导出用户数据", "窃取数据", "窃取用户",
    "绕过验证", "bypass", "爬取数据", "抓取数据", "爬虫",
    # ── 刷量 / 水军 / 批量注册(灰产) ──
    "水军", "刷量", "刷单", "刷评论", "刷赞", "刷粉", "刷屏", "刷流量",
    "批量注册", "批量注册账号", "批量注册小号", "注册小号", "养号", "小号",
    "打码平台", "接码平台", "验证码绕过",
    # ── 钓鱼 / 木马 / 勒索 / 爆破 ──
    "钓鱼", "phishing", "木马", "病毒", "后门", "勒索", "勒索病毒",
    "暴力破解", "爆破", "字典攻击", "ddos", "拒绝服务攻击", "封禁对方", "举报对方",
    # ── 系统级危险 ──
    "关机", "shutdown",
})

# SOFT critical: 出现在"做X功能/页面"语境中是正常建站, 否则视为风险
SAFETY_SOFT_CRITICAL = frozenset({
    "删除", "支付", "付款", "充值", "订单", "交易", "转账",
    "密码", "密钥", "token", "api_key", "secret",
    "银行卡", "信用卡", "credit card", "身份证", "ssn", "私钥", "凭证", "credential",
    "rm", "remove", "del", "delete",
})

# SOFT high
SAFETY_SOFT_HIGH = frozenset({
    # ── 发布 / 部署 ──
    "发布", "上线", "deploy", "publish",
    "管理", "admin", "后台",
    "修改权限", "更改角色",
    # ── 数据迁移 / 配置变更 ──
    "导出", "export", "备份", "backup", "迁移", "migrate",
    "升级", "upgrade", "重启", "restart", "停止服务", "stop",
    "提交", "commit", "合并", "merge",
    "封禁", "ban", "删除用户", "移除用户", "改密码", "重置密码",
    "grant", "revoke", "权限", "用户管理",
})

# SOFT medium
SAFETY_SOFT_MEDIUM = frozenset({
    "修改", "改", "modify", "update", "更新",
    "新增", "添加", "add", "create",
    # ── 编辑 / 配置类 ──
    "调整", "微调", "编辑", "edit", "配置", "config",
    "重命名", "rename", "设置", "改色", "换色", "排版", "布局调整",
})

# 建设性前导(verb-ish): 出现在 SOFT 关键词之前(窗口内), 表示"让我做一个X"(前导词)
#  - CONSTRUCTIVE_LEADS: 完整集(含"帮我/请帮我"等通用请求语), 用于 critical/medium 中和。
#  - STRICT_LEADS: 仅"做功能/页面"类强前导, 用于 high(避免"帮我删除用户"被误中和)。
CONSTRUCTIVE_LEADS = (
    "帮我", "帮我做", "帮我写", "帮我加", "帮我生成", "帮我创建", "帮我设计",
    "帮我开发", "帮我实现", "做一个", "开发一个", "加一个", "添加一个", "实现一个",
    "生成", "创建", "设计", "写", "加", "做", "开发", "实现",
    "需要", "想要", "我想", "请帮我", "请", "帮我把", "给我做",
)
STRICT_LEADS = (
    "帮我做", "帮我写", "帮我加", "帮我生成", "帮我创建", "帮我设计",
    "帮我开发", "帮我实现", "做一个", "开发一个", "加一个", "添加一个", "实现一个",
    "生成", "创建", "设计", "写", "加", "做", "开发", "实现",
)

# UI / 代码语境词(noun-ish): 出现在 SOFT 关键词附近, 表示"这是一个界面/代码元素"(附近词)
SAFETY_UI_CONTEXT = (
    "页面", "组件", "表单", "按钮", "输入框", "输入", "模块", "功能", "界面",
    "框", "栏", "卡片", "弹窗", "菜单", "列表", "表格",
    "注释", "代码", "文件", "函数", "变量", "样式", "文案", "字段", "区域",
)

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
