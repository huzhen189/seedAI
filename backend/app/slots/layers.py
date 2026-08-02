"""分层槽位体系（L0/L1/L2/L3）—— 建站信息收集的单一真相源（确定性、可 CI 校验）。

设计要点（来自需求评审决策）：
  - L0 全局通用：必填只放「网站名 / 样式风格 / 内容主题 / 部署目标」；可选放品牌色/字体/多语言/SEO；
    隐式放项目根目录/技术栈(Vue3 默认)。**不再把联系电话/邮箱/语言当通用**（用户决策：这些不能算作通用）。
  - L1 行业通用：只分 **3 个粗分类**（content_showcase / ecommerce_service / interactive_platform），
    每类只给少量可选槽，**绝不要求过细**（用户决策：不能够要求太细致）。
  - L2 网站类型：组合继承。canonical 值来自 intent_config.json 的 site_type
    （corporate/portfolio/personal/blog/commerce/landing），每类绑定一个 L1 桶并可选追加类型特有槽。
  - L3 动态业务槽：用户自定义 / Planner 推断，靠 ``extend()`` 机制产生，**不预定义、不爆炸**。
  - 行业枚举：首版覆盖热门 **50 个行业**（`INDUSTRY_BUCKETS`），每个映射到 3 个桶之一。
    长尾/未知行业由向量库（industry_slots 集合）做语义召回兜底——但本模块只持有确定性映射。

与现有管线的关系：
  - 槽位真相源 = 本注册表（确定性、可 CI）。向量库只做 enrichment（引导），不存槽位真相。
  - 实际填充仍走 S3 DST（``sir.slots`` 合并）；``extend()`` 动态槽落 ``sir.slots``（``dyn_`` 前缀）。
"""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field


class LayerKind(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class SlotKind(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    IMPLICIT = "implicit"


class SlotDef(BaseModel):
    """单个槽位定义。``key`` 全局唯一（靠前缀避免冲突：L0/L1/L2 用语义名，L3 强制 ``dyn_`` 前缀）。"""

    key: str
    label: str
    layer: LayerKind
    kind: SlotKind
    prompt_hint: str = ""
    example: str | None = None
    validation: dict = Field(default_factory=dict)
    # 上层 key 继承提示（仅 L1/L2 使用，便于 CI 校验层级良构）。
    inherits: list[str] = Field(default_factory=list)


# -------------------------------------------------------------------------------- L0 全局通用
# 必填只放用户明确指定的 4 项；联系/邮箱/语言一律不进通用层。
L0_REQUIRED: list[SlotDef] = [
    SlotDef(key="site_name", label="网站名称", layer=LayerKind.L0, kind=SlotKind.REQUIRED,
            prompt_hint="询问用户这个网站叫什么名字", example="花间集"),
    SlotDef(key="style", label="样式风格", layer=LayerKind.L0, kind=SlotKind.REQUIRED,
            prompt_hint="确认整体视觉风格（如简约 / 商务 / 活泼 / 科技感）", example="简约商务"),
    SlotDef(key="primary_goal", label="内容主题 / 主要目的", layer=LayerKind.L0, kind=SlotKind.REQUIRED,
            prompt_hint="网站主要展示什么内容或想达成什么目的", example="展示烘焙教程与食谱"),
    SlotDef(key="deploy_target", label="部署目标", layer=LayerKind.L0, kind=SlotKind.REQUIRED,
            prompt_hint="网站打算部署到哪里（如自有域名 / 平台托管 / 本地预览）", example="平台托管"),
]
L0_OPTIONAL: list[SlotDef] = [
    SlotDef(key="brand_color", label="品牌主色", layer=LayerKind.L0, kind=SlotKind.OPTIONAL,
            prompt_hint="是否有偏好的品牌主色", example="#1A73E8"),
    SlotDef(key="font_family", label="字体偏好", layer=LayerKind.L0, kind=SlotKind.OPTIONAL,
            prompt_hint="是否对字体有偏好", example="无衬线"),
    SlotDef(key="i18n", label="多语言", layer=LayerKind.L0, kind=SlotKind.OPTIONAL,
            prompt_hint="是否需要多语言版本", example="中英双语"),
    SlotDef(key="seo_keywords", label="SEO 关键词", layer=LayerKind.L0, kind=SlotKind.OPTIONAL,
            prompt_hint="是否有希望被搜索命中关键词", example="烘焙教程"),
]
L0_IMPLICIT: list[SlotDef] = [
    SlotDef(key="project_root", label="项目根目录", layer=LayerKind.L0, kind=SlotKind.IMPLICIT,
            prompt_hint="系统自动填充，无需询问"),
    SlotDef(key="tech_stack", label="技术栈", layer=LayerKind.L0, kind=SlotKind.IMPLICIT,
            prompt_hint="默认 Vue3，系统自动填充", example="Vue3"),
]

# -------------------------------------------------------------------------------- L1 行业通用（仅 3 粗分类，每类少量可选）
L1_BUCKETS: dict[str, list[SlotDef]] = {
    "content_showcase": [  # 内容展示类：作品集/博客/资讯/官网/个人
        SlotDef(key="showcase_sections", label="展示板块", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="想展示哪些板块（如关于/作品/文章）", example="关于、作品、联系方式"),
        SlotDef(key="update_frequency", label="更新频率", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="内容大概多久更新一次", example="每周"),
    ],
    "ecommerce_service": [  # 电商服务类：商城/餐饮/零售/教育/医疗/预约
        SlotDef(key="payment_methods", label="收款 / 支付方式", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="需要支持哪些支付方式", example="微信 / 支付宝"),
        SlotDef(key="business_hours", label="营业 / 服务时间", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="营业或服务时间", example="9:00-21:00"),
    ],
    "interactive_platform": [  # 交互平台类：社区/后台/工具/SaaS/会员
        SlotDef(key="user_auth_mode", label="用户登录方式", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="用户如何登录（手机号/邮箱/第三方）", example="手机号"),
        SlotDef(key="core_interactions", label="核心交互", layer=LayerKind.L1, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="平台最核心的几种交互行为", example="发帖 / 评论 / 下单"),
    ],
}

# -------------------------------------------------------------------------------- L2 网站类型（组合继承，canonical 值来自 intent_config.json）
# 每类绑定一个 L1 桶（继承其可选槽），可追加类型特有可选槽。
L2_TYPES: dict[str, dict] = {
    "corporate": {"bucket": "content_showcase", "extra": []},
    "portfolio": {"bucket": "content_showcase", "extra": []},
    "personal": {"bucket": "content_showcase", "extra": []},
    "blog": {"bucket": "content_showcase", "extra": [
        SlotDef(key="blog_categories", label="博客分类", layer=LayerKind.L2, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="博客想分哪些类目", example="技术 / 生活"),
    ]},
    "commerce": {"bucket": "ecommerce_service", "extra": [
        SlotDef(key="product_categories", label="商品类目", layer=LayerKind.L2, kind=SlotKind.OPTIONAL,
                inherits=["site_name"], prompt_hint="主要卖哪些类目的商品", example="服装 / 数码"),
    ]},
    "landing": {"bucket": "content_showcase", "extra": []},
}

# -------------------------------------------------------------------------------- 行业→桶映射（首版热门 50 行业）
# key=命中关键词（小写匹配），value=(展示名, 桶)。多关键词可映射到同一行业/桶。
INDUSTRY_BUCKETS: dict[str, tuple[str, str]] = {
    # —— content_showcase（17）——
    "作品集": ("作品集", "content_showcase"),
    "博客": ("博客", "content_showcase"),
    "新闻": ("新闻资讯", "content_showcase"),
    "企业官网": ("企业官网", "content_showcase"),
    "个人主页": ("个人主页", "content_showcase"),
    "摄影": ("摄影", "content_showcase"),
    "艺术": ("艺术", "content_showcase"),
    "文学": ("文学写作", "content_showcase"),
    "音乐": ("音乐", "content_showcase"),
    "影视": ("影视视频", "content_showcase"),
    "旅游": ("旅游攻略", "content_showcase"),
    "时尚": ("时尚", "content_showcase"),
    "设计": ("设计", "content_showcase"),
    "教育培训": ("教育培训", "content_showcase"),
    "文化": ("文化非遗", "content_showcase"),
    "科研": ("科研学术", "content_showcase"),
    "公益": ("公益非营利", "content_showcase"),
    # —— ecommerce_service（18）——
    "餐饮": ("餐饮", "ecommerce_service"),
    "零售": ("零售商店", "ecommerce_service"),
    "电商": ("电商商城", "ecommerce_service"),
    "咖啡": ("咖啡", "ecommerce_service"),
    "烘焙": ("烘焙", "ecommerce_service"),
    "服装": ("服装", "ecommerce_service"),
    "数码": ("数码3C", "ecommerce_service"),
    "美业": ("美业美容", "ecommerce_service"),
    "健身": ("健身", "ecommerce_service"),
    "医疗": ("医疗诊所", "ecommerce_service"),
    "药店": ("药店", "ecommerce_service"),
    "酒店": ("酒店", "ecommerce_service"),
    "旅游预订": ("旅游预订", "ecommerce_service"),
    "票务": ("票务", "ecommerce_service"),
    "生鲜": ("生鲜", "ecommerce_service"),
    "农产品": ("农产品", "ecommerce_service"),
    "珠宝": ("珠宝", "ecommerce_service"),
    "花店": ("花店", "ecommerce_service"),
    # —— interactive_platform（15）——
    "社区": ("社区论坛", "interactive_platform"),
    "论坛": ("社区论坛", "interactive_platform"),
    "后台": ("后台管理", "interactive_platform"),
    "saas": ("SaaS工具", "interactive_platform"),
    "会员": ("会员系统", "interactive_platform"),
    "在线课程": ("在线课程", "interactive_platform"),
    "招聘": ("招聘HR", "interactive_platform"),
    "咨询": ("咨询", "interactive_platform"),
    "客服": ("客服系统", "interactive_platform"),
    "直播": ("直播", "interactive_platform"),
    "游戏": ("游戏", "interactive_platform"),
    "小程序": ("小程序", "interactive_platform"),
    "内部系统": ("内部系统", "interactive_platform"),
    "数据看板": ("数据看板", "interactive_platform"),
    "知识库": ("知识库", "interactive_platform"),
}


def detect_industry(message: str) -> str | None:
    """从用户消息检测行业关键词。

    策略：取**最早出现**的关键词（位置优先），同位置取较长者。
    这样「摄影作品集」会命中「摄影」而非被更长的「作品集」抢走，更符合直觉。
    返回命中关键词或 ``None``。
    """
    if not message:
        return None
    text = message.lower()
    best: str | None = None
    best_idx: int | None = None
    for kw in INDUSTRY_BUCKETS:
        idx = text.find(kw.lower())
        if idx == -1:
            continue
        if best_idx is None or idx < best_idx or (idx == best_idx and len(kw) > len(best or "")):
            best, best_idx = kw, idx
    return best


# -------------------------------------------------------------------------------- L3 动态业务槽触发词
# 命中关键词 → 一个 L3 动态业务槽（由 ``extend()`` 产生，强制 ``dyn_`` 前缀）。
# 语义：用户在某句话里提到这些业务概念（会员 / 积分 / 预约 / 优惠 …），系统自动把对应
# 业务槽加入「待收集栈」(slot_stack)，并由 S3 沉淀为该用户/项目的持久偏好（含更新语义）
# —— 闭环见 ``persist.py`` 与 ``stages/s3_dst.py``。
DYNAMIC_SLOT_TRIGGERS: dict[str, tuple[str, str, str]] = {
    # 触发词(小写匹配) -> (原始 key, label, prompt_hint)
    "会员": ("membership_tiers", "会员等级体系", "需要设置哪些会员等级及对应权益"),
    "积分": ("points_system", "积分体系", "积分如何获取与消耗"),
    "预约": ("booking_slots", "预约时段", "可预约的时间段与容量"),
    "优惠券": ("coupon_rules", "优惠规则", "优惠券的发放条件与折扣方式"),
    "优惠": ("coupon_rules", "优惠规则", "希望提供哪些优惠方式"),
    "分销": ("distribution", "分销层级", "分销的层级与佣金规则"),
    "课程": ("course_schedule", "课程安排", "课程设置与排期方式"),
    "报名": ("form_fields", "报名表单字段", "报名需收集哪些字段"),
    "表单": ("form_fields", "表单字段", "需要哪些表单与字段"),
    "物流": ("shipping_zones", "配送区域", "配送覆盖区域与运费规则"),
    "配送": ("shipping_zones", "配送区域", "配送范围与时效"),
    "评论": ("review_attrs", "评价维度", "用户评价包含哪些维度"),
    "多语言": ("locales", "语言版本", "需要支持哪些语言版本"),
    "国际化": ("locales", "语言版本", "面向哪些地区 / 语言"),
}


def detect_dynamic_slots(message: str) -> list[SlotDef]:
    """从用户消息检测 L3 动态业务槽触发词，返回对应的 ``extend()`` 槽位列表（按 key 去重）。

    不依赖 LLM / 网络，仅做关键词命中。返回的槽位会经 ``compose(dynamic=...)``
    进入 ``slot_stack``，使 AI 在后续轮次收集该业务信息，并由 S3 持久化。
    """
    if not message:
        return []
    text = message.lower()
    seen: dict[str, SlotDef] = {}  # raw_key -> SlotDef（去重，避免「优惠」与「优惠券」重复注入）
    for kw, (raw_key, label, hint) in DYNAMIC_SLOT_TRIGGERS.items():
        if kw.lower() in text and raw_key not in seen:
            seen[raw_key] = extend(raw_key, label, prompt_hint=hint)
    return list(seen.values())


def _buckets_for(industry: str | None, site_types: list[str] | None) -> list[str]:
    """收集本次应纳入的 L1 桶集合（类型优先 + 行业补充 + 默认兜底）。"""
    buckets: set[str] = set()
    for t in (site_types or []):
        spec = L2_TYPES.get(t)
        if spec:
            buckets.add(spec["bucket"])
    if industry:
        bucket = INDUSTRY_BUCKETS.get(industry, (None, "content_showcase"))[1]
        buckets.add(bucket)
    if not buckets:
        # 完全无信号时，默认按内容展示类兜底（绝大多数网站属于此类）。
        buckets.add("content_showcase")
    return list(buckets)


def compose(
    industry: str | None = None,
    site_types: list[str] | None = None,
    dynamic: list[SlotDef] | None = None,
) -> "SlotStack":
    """拼装槽位栈：L0 → 行业/类型对应 L1 桶 → 类型特有槽 → L3 动态槽。

    同名 key 后者覆盖前者（L2 可覆盖 L0 同名；L3 动态槽因 ``dyn_`` 前缀天然不冲突）。
    """
    merged: dict[str, SlotDef] = {}

    def add(defs: list[SlotDef]) -> None:
        for d in defs:
            merged[d.key] = d  # 后者覆盖

    add(L0_REQUIRED)
    add(L0_OPTIONAL)
    add(L0_IMPLICIT)

    for bucket in _buckets_for(industry, site_types):
        add(L1_BUCKETS.get(bucket, []))
    for t in (site_types or []):
        spec = L2_TYPES.get(t)
        if spec:
            add(spec["extra"])

    if dynamic:
        add(dynamic)

    return SlotStack(slots=list(merged.values()))


def extend(slot_key: str, label: str, prompt_hint: str = "", example: str | None = None) -> SlotDef:
    """产生一个 L3 动态业务槽。``slot_key`` 强制 ``dyn_`` 前缀，避免与 L0/L1/L2 冲突。"""
    if not slot_key.startswith("dyn_"):
        slot_key = "dyn_" + slot_key
    return SlotDef(
        key=slot_key, label=label, layer=LayerKind.L3, kind=SlotKind.OPTIONAL,
        prompt_hint=prompt_hint, example=example,
    )


class SlotStack(BaseModel):
    """一次组合后的完整槽位栈。"""

    slots: list[SlotDef] = Field(default_factory=list)

    @property
    def required(self) -> list[SlotDef]:
        return [s for s in self.slots if s.kind == SlotKind.REQUIRED]

    @property
    def optional(self) -> list[SlotDef]:
        return [s for s in self.slots if s.kind == SlotKind.OPTIONAL]

    @property
    def implicit(self) -> list[SlotDef]:
        return [s for s in self.slots if s.kind == SlotKind.IMPLICIT]

    def as_dict(self) -> dict:
        return {
            "required": [s.key for s in self.required],
            "optional": [s.key for s in self.optional],
            "implicit": [s.key for s in self.implicit],
            "all": [s.key for s in self.slots],
        }

    def guidance(self, filled: set[str] | None = None) -> dict:
        """生成「待收集」引导：必填未填 + 可选建议。``filled`` 为已填充的 key 集合。"""
        filled = filled or set()
        missing_req = [s.label for s in self.required if s.key not in filled]
        suggested_opt = [s.label for s in self.optional if s.key not in filled]
        return {
            "missing_required": missing_req,
            "suggested_optional": suggested_opt,
            "questions": [s.prompt_hint for s in self.required if s.key not in filled and s.prompt_hint],
        }
