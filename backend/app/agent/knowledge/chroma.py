"""RAG 检索增强(②-a · 文档 §7):多集合 Chroma 索引(v0.9.0 六集合扩展)。

- `build_rag_context`:检索 components(组件库)+ memory(历史记忆),拼为 Planner 可用上下文。
- `save_memory`:生成成功后异步回写 memory 集合(记忆闭环)。
- `seed_components`:批量写入 components 集合(数据准备,由 scripts/seed_rag_components.py 调用)。
- 新增集合: user_preferences / project_memory / project_code / error_patterns (§四)

依赖:chromadb + Qwen text-embedding(§7 已配)。embedding key / chroma 不可用时**优雅降级**
(返回空上下文 / 跳过回写),不阻断主生成流。
"""

from __future__ import annotations

import hashlib
import logging

from ..config import settings


logger = logging.getLogger("ai_service.chroma")

# 注入 Planner 的 RAG 上下文上限,防 prompt 过长(3.4 新增项目记忆/用户偏好/错误经验后放宽)
_RAG_INJECT_MAX_CHARS = 6000

# 对话上下文集合(向量相似度边界检测)。提到顶部,供 _ALL_COLLECTIONS 引用。
CTX_COLLECTION = "conversation_context"
# 余弦相似度 < 阈值 视为无关。注意: 阈值需匹配实际 embedding 距离尺度——
# Qwen text-embedding-v3 下"近重复"消息的余弦距离约 0.47(sim≈0.53),
# 原 0.55 会把几乎所有相关历史拒之门外, 导致 3.3 上下文连贯形同虚设。
# 实测 0.40 能放行主题相关消息、过滤无关噪声。
CTX_SIMILARITY_THRESHOLD = 0.40


_CLIENT = None  # 进程内单例: 复用同一 HTTP 连接, 避免每次调用重建客户端


def _client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    from urllib.parse import urlparse

    import chromadb

    p = urlparse(settings.chroma_url)
    _CLIENT = chromadb.HttpClient(host=p.hostname or "localhost", port=p.port or 8000)
    return _CLIENT


def _ef():
    from chromadb.utils import embedding_functions

    # 优先 Qwen text-embedding(§7: 配 qwen_embedding_key)。
    # 关键点: api_base 必须用 settings.qwen_embedding_base_url(本机 Qwen 嵌入 key 是 ws 私有
    # MaaS 工作区主机签发, 打聊天 token-plan 端点会 401)。无 embedding key 时本地 sentence-transformers 兜底。
    if settings.qwen_embedding_key:
        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.qwen_embedding_key,
            api_base=settings.qwen_embedding_base_url,
            model_name=settings.qwen_embedding_model,
        )
    # 本地模型兜底(无需 API key, 首次使用自动下载 all-MiniLM-L6-v2 ~79MB)
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )


def _available() -> bool:
    try:
        return _ef() is not None
    except Exception:
        return False


# 全部集合名(单一事实来源)。重置数据会 delete_collection 删空集合,
# ensure_collections() 据此在 AI 服务启动时确定性地重建向量库"结构"。
_ALL_COLLECTIONS = [
    settings.chroma_collection_components,
    settings.chroma_collection_memory,
    settings.chroma_collection_cache,
    settings.chroma_collection_user_preferences,
    settings.chroma_collection_project_memory,
    settings.chroma_collection_project_code,
    settings.chroma_collection_error_patterns,
    settings.chroma_collection_intents,
    CTX_COLLECTION,
]


def ensure_collections() -> None:
    """确保全部 Chroma 集合存在(用统一 _ef 创建)。

    重置数据后会删空集合, 本函数在 AI 服务启动时调用, 确定性地重建向量库
    '结构', 避免首个 RAG / 上下文检索调用前因集合缺失而 404。
    用 get_or_create_collection, 集合已存在则不报错(且 ef 一致, 不会触发
    embedding 函数不匹配)。
    """
    if not _available():
        logger.warning("ensure_collections: embedding 不可用, 跳过(向量库降级)")
        return
    try:
        client = _client()
        ef = _ef()
        provider = "Qwen text-embedding-v3" if settings.qwen_embedding_key else "本地 SentenceTransformer(all-MiniLM-L6-v2)"
        logger.info("ensure_collections: embedding 提供方=%s", provider)
        for name in _ALL_COLLECTIONS:
            try:
                client.get_or_create_collection(name=name, embedding_function=ef)
            except Exception as e:
                logger.warning("ensure_collections: 创建 %s 失败: %s", name, e)
        logger.info("ensure_collections: 已确保 %d 个集合存在", len(_ALL_COLLECTIONS))
        # 冷启动种子: components(组件库参考) / error_patterns(错误模式经验) 为空时填充,
        # 保证 3.2 组件库 RAG 与 3.4 错误经验库从首次启动就可用。幂等(固定 id, 重复 upsert 覆盖)。
        # 注: 此前 scripts/seed_rag_components.py 缺失导致两集合长期为 0, 组件库 RAG 与错误经验始终是空操作。
        try:
            _seed_bootstrap()
        except Exception as _se:  # noqa: BLE001
            logger.warning("ensure_collections: 冷启动种子失败(可忽略): %s", _se)
    except Exception as e:
        logger.warning("ensure_collections 失败(可忽略): %s", e)


def _short_hash(s: str) -> str:
    """稳定短哈希, 用于 Chroma id 后缀."""
    return hashlib.md5(s.encode()).hexdigest()[:8]


# ---- 通用检索 ----

def retrieve(query: str, collection: str, top_k: int | None = None) -> list[dict]:
    """在指定 collection 语义检索,返回 [{content, metadata, score}]。不可用则返回 []。"""
    if not _available():
        return []
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        res = col.query(query_texts=[query], n_results=top_k or settings.rag_top_k)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [
            {"content": d, "metadata": m, "score": (1 - s) if s is not None else None}
            for d, m, s in zip(docs, metas, dists)
        ]
    except Exception as e:
        logger.warning("rag retrieve(%s) failed: %s", collection, e)
        return []


def _retrieve_where(query: str, collection: str, where: dict, top_k: int | None = None) -> list[dict]:
    """带 metadata where 过滤的语义检索。"""
    if not _available():
        return []
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        res = col.query(
            query_texts=[query], n_results=top_k or settings.rag_top_k, where=where,
        )
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [
            {"content": d, "metadata": m, "score": (1 - s) if s is not None else None}
            for d, m, s in zip(docs, metas, dists)
        ]
    except Exception as e:
        logger.warning("rag _retrieve_where(%s, %s) failed: %s", collection, where, e)
        return []


def _upsert(collection: str, ids: list[str], docs: list[str], metas: list[dict]) -> None:
    """通用 upsert, 失败仅 warn。

    注意: Qwen text-embedding-v3 的批量 embedding 接口单批上限 10 条,
    超过会返回 400 (batch size is invalid, it should not be larger than 10)。
    因此强制按 10 条分块 upsert, 否则 seed_components/seed_error_patterns(各 20 条) 会整体失败。
    """
    if not _available():
        return
    try:
        col = _client().get_or_create_collection(name=collection, embedding_function=_ef())
        BATCH = 10
        for i in range(0, len(ids), BATCH):
            col.upsert(
                ids=ids[i:i + BATCH],
                documents=docs[i:i + BATCH],
                metadatas=metas[i:i + BATCH],
            )
    except Exception as e:
        logger.warning("rag _upsert(%s, %d) failed: %s", collection, len(ids), e)


# ---- 原有集合(components / memory) ----

def build_rag_context(query: str, project_id: int | None = None,
                      user_id: int | None = None) -> str:
    """检索 components + memory + 用户偏好 + 项目记忆 + 错误模式,拼接为 Planner 可用上下文字符串(空则返回 '')。
    v0.9.0+: user_id 检索 user_preferences(个性化), project_id 检索 project_memory(项目隔离),
    error_patterns 全局检索(错误经验复用)。所有检索优雅降级,失败不阻断生成。"""
    if not _available():
        return ""
    logger.info("[RAG] build_rag_context 入口 query=%.60s project_id=%s user_id=%s",
                query, project_id, user_id)
    parts: list[str] = []
    # ── 组件库参考(全局) ──
    comps = retrieve(query, settings.chroma_collection_components)
    if comps:
        snippets = "\n\n".join(f"- {c['content']}" for c in comps)
        parts.append(f"【组件库参考】\n{snippets}")
        logger.info("[RAG] components 命中 %d 条(注入 %d 字), top=%.120s",
                    len(comps), len(snippets), comps[0]["content"])
    else:
        logger.info("[RAG] components 未命中")
    # ── 历史记忆(可按 project_id 隔离,避免跨项目污染) ──
    if project_id is not None:
        mems = _retrieve_where(
            query, settings.chroma_collection_memory,
            where={"project_id": project_id},
        )
    else:
        mems = retrieve(query, settings.chroma_collection_memory)
    if mems:
        snippets = "\n\n".join(f"- {m['content']}" for m in mems)
        parts.append(f"【历史记忆】\n{snippets}")
        logger.info("[RAG] memory 命中 %d 条(project_id=%s, 注入 %d 字)",
                    len(mems), project_id, len(snippets))
    else:
        logger.info("[RAG] memory 未命中(project_id=%s)", project_id)
    # ── 项目记忆(3.4, 按 project_id 隔离) ──
    if project_id is not None:
        pmems = retrieve_project_memory(project_id, query)
        if pmems:
            snippets = "\n\n".join(f"- {p['content']}" for p in pmems)
            parts.append(f"【项目记忆】\n{snippets}")
            logger.info("[RAG] project_memory 命中 %d 条(project_id=%s, 注入 %d 字)",
                        len(pmems), project_id, len(snippets))
        else:
            logger.info("[RAG] project_memory 未命中(project_id=%s)", project_id)
    # ── 用户偏好(3.4, 按 user_id 隔离) ──
    if user_id is not None:
        prefs = retrieve_user_preferences(user_id, query)
        if prefs:
            snippets = "\n\n".join(f"- {p['content']}" for p in prefs)
            parts.append(f"【用户偏好】\n{snippets}")
            logger.info("[RAG] user_preferences 命中 %d 条(user_id=%s, 注入 %d 字)",
                        len(prefs), user_id, len(snippets))
        else:
            logger.info("[RAG] user_preferences 未命中(user_id=%s)", user_id)
    # ── 错误模式经验(3.4, 全局复用) ──
    errs = retrieve_error_patterns(query)
    if errs:
        snippets = "\n\n".join(f"- {e['content']}" for e in errs)
        parts.append(f"【错误模式经验】\n{snippets}")
        logger.info("[RAG] error_patterns 命中 %d 条(注入 %d 字)", len(errs), len(snippets))
    else:
        logger.info("[RAG] error_patterns 未命中")
    ctx = "\n\n".join(parts)
    if ctx:
        logger.info("[RAG] 注入 Planner 上下文总长=%d 字(有增益)", len(ctx))
    else:
        logger.info("[RAG] 无 RAG 增益(所有集合均未命中)")
    return ctx[:_RAG_INJECT_MAX_CHARS] if ctx else ""


def save_memory(trace_id: str, title: str, content: str,
                tags: list[str] | None = None,
                project_id: int | None = None,
                user_id: int | None = None) -> None:
    """生成成功后回写 memory 集合(②-a 记忆闭环)。失败仅记录,不阻断。
    v0.9.0: 新增 project_id / user_id (可选, 用于按项目隔离)。"""
    if not _available():
        return
    try:
        col = _client().get_or_create_collection(
            name=settings.chroma_collection_memory, embedding_function=_ef()
        )
        summary = (title + "\n" + content)[:2000]
        meta = {"trace_id": trace_id, "title": title, "tags": ",".join(tags or [])}
        if project_id is not None:
            meta["project_id"] = project_id
        if user_id is not None:
            meta["user_id"] = user_id
        col.upsert(
            ids=[f"mem_{trace_id}"],
            documents=[summary],
            metadatas=[meta],
        )
    except Exception as e:
        logger.warning("rag save_memory failed: %s", e)


def get_collection(name: str):
    """获取(或创建)指定 Chroma 集合(供数据准备脚本使用)。"""
    return _client().get_or_create_collection(name=name, embedding_function=_ef())


# 组件库种子(冷启动写入 components 集合,供 3.2 建站 RAG 参考)。
# 每条 = 一个可复用前端组件/模式的"设计意图 + 关键实现要点",帮助 Planner 产出更一致、更优质的代码。
_COMPONENT_SEEDS: list[dict] = [
    {"content": "玻璃拟态卡片(Glassmorphism Card):半透明背景 + backdrop-filter:blur(20px) + 1px 半透明白边 + 圆角 16-20px。用于功能卡/数据卡。深色背景上效果最佳,需确保底层有渐变或图像以透出。", "metadata": {"type": "card", "tags": "glass,blur,premium"}},
    {"content": "Hero 区块:视口高度 70-90vh,左对齐大标题(字号 clamp(2.5rem,6vw,4.5rem))+副文案+主CTA按钮,背景用渐变或粒子/图像。移动端标题缩小、CTA 纵向堆叠。", "metadata": {"type": "layout", "tags": "hero,landing"}},
    {"content": "响应式导航栏:桌面横向 flex 分布 logo+链接+CTA,sticky top:0 + 半透明毛玻璃;移动端汉堡菜单(点击展开全屏或下拉面板),用 max-width 媒体查询断点 768px。", "metadata": {"type": "nav", "tags": "navigation,responsive"}},
    {"content": "深浅主题切换:用 CSS 变量(--bg/--fg/--accent)定义调色板,html[data-theme='dark'] 覆盖为深色值;切换按钮写 data-theme 到 <html> 并持久化 localStorage。过渡 transition:background-color .3s。", "metadata": {"type": "theme", "tags": "darkmode,theme-toggle"}},
    {"content": "CSS Grid 自适应画廊:grid-template-columns:repeat(auto-fill,minmax(240px,1fr)) + gap;子项 aspect-ratio 固定避免变形;图片 object-fit:cover。避免 grid 列用 1fr 导致内容超宽——用 minmax(0,1fr)。", "metadata": {"type": "grid", "tags": "gallery,grid"}},
    {"content": "Flex 容器防溢出:flex 子项必须 min-width:0(或 overflow:hidden)否则长内容会撑破布局。横向滚动容器用 overflow-x:auto + scroll-snap-type:x mandatory 做吸附。", "metadata": {"type": "css", "tags": "flex,overflow"}},
    {"content": "主按钮(magnetic/渐变):background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:999px;padding:.8rem 1.6rem;hover 时 transform:translateY(-2px)+box-shadow 提升;transition 用 cubic-bezier(.16,1,.3,1)。", "metadata": {"type": "button", "tags": "cta,hover"}},
    {"content": "表单:每个 input 配 <label for>;聚焦用 :focus-visible 显示轮廓;错误态红边+提示文字;提交按钮禁用态降透明度。移动端输入框 width:100%。", "metadata": {"type": "form", "tags": "form,a11y"}},
    {"content": "统计数字区:网格排列 3-4 个 KPI,数字用大号粗体渐变文字,下方小字标签;入场用 count-up 或简单 fade-in(IntersectionObserver 触发)。", "metadata": {"type": "stats", "tags": "metrics,animation"}},
    {"content": "特性列表(图文交错):左右两栏,奇数行图左文右、偶数行反之;图用圆角卡片包裹;移动端单列堆叠、图在上。间距用 section padding clamp(3rem,8vw,6rem)。", "metadata": {"type": "feature", "tags": "alternating,section"}},
    {"content": "页脚(Footer):多列链接(产品/资源/公司/法律)+ 社交图标 + 版权;顶部细分隔线;border-top:1px solid rgba(255,255,255,.1);移动端列变纵向。", "metadata": {"type": "footer", "tags": "footer"}},
    {"content": "Toast/通知:固定右上或底部,圆角卡片 + 图标 + 自动消失(3-4s),用 transform+opacity 过渡;z-index 高于内容但低于模态。", "metadata": {"type": "feedback", "tags": "toast,notification"}},
    {"content": "模态弹窗:全屏半透明遮罩 rgba(0,0,0,.5)+居中卡片;ESC/点击遮罩关闭;卡片 max-width + max-height:90vh + overflow:auto;入场 scale(.96)→1 + fade。", "metadata": {"type": "modal", "tags": "dialog,overlay"}},
    {"content": "时间轴(Timeline):竖线 + 节点圆点 + 两侧卡片;移动端单侧左对齐。节点用 accent 色,激活项放大。", "metadata": {"type": "timeline", "tags": "timeline"}},
    {"content": "标签页(Tabs):一组等宽标签 + 下划线指示器(active 用 accent),内容区切换;移动端可横向滚动。键盘左右方向键切换(role=tab)。", "metadata": {"type": "tabs", "tags": "tabs,a11y"}},
    {"content": "滚动入场动画:IntersectionObserver 监听 [data-reveal],进入视口加 .in 类触发 opacity:0→1 + translateY(20px)→0,过渡 .6s;尊重 prefers-reduced-motion:reduce 时禁用。", "metadata": {"type": "animation", "tags": "scroll,reveal,a11y"}},
    {"content": "面包屑(Breadcrumb):用 <nav aria-label='breadcrumb'> + 分隔符 /,最后一项 aria-current='page'。移动端可省略中间层级。", "metadata": {"type": "nav", "tags": "breadcrumb,a11y"}},
    {"content": "无障碍基础:所有可点击元素 ≥44x44px;图片 alt 必填;颜色对比度 ≥4.5:1;标题层级 h1→h2→h3 不跳级;交互元素同时有 :hover 与 :focus-visible。", "metadata": {"type": "a11y", "tags": "accessibility,wcag"}},
    {"content": "定价卡片(Pricing):3 档(基础/推荐/专业),推荐档加 '最受欢迎' 徽标 + accent 边框放大;列对齐特性清单(✓/✗);CTA 各自按钮。移动端纵向堆叠。", "metadata": {"type": "pricing", "tags": "pricing,card"}},
    {"content": "视频/图像背景 Hero:background 用 <video autoplay muted loop> 或封面图 + 深色遮罩保证文字可读;文字加 text-shadow 或底板。移动端降级为静态图省流量。", "metadata": {"type": "hero", "tags": "video-bg,hero"}},
]


def seed_components(items: list[dict]) -> int:
    """批量写入 components 集合;items=[{content, metadata}]。返回写入条数。"""
    if not items:
        return 0
    ids = [f"comp_{i}" for i in range(len(items))]
    docs = [it["content"] for it in items]
    metas = [it.get("metadata", {}) for it in items]
    # 走 _upsert(已按 ≤10 分批, 绕过 Qwen embedding 单批上限)
    _upsert(settings.chroma_collection_components, ids, docs, metas)
    return len(ids)


# ---- 对话上下文关联(向量相似度边界检测) ----


def index_message(msg_id: int, conversation_id: int, role: str, content: str) -> None:
    """将消息写入 Chroma 上下文集合(供相似度检测)。"""
    if not _available() or not content.strip():
        return
    try:
        col = _client().get_or_create_collection(name=CTX_COLLECTION, embedding_function=_ef())
        col.upsert(
            ids=[f"msg_{msg_id}"],
            documents=[content[:2000]],
            metadatas=[{"conversation_id": conversation_id, "role": role, "msg_id": msg_id}],
        )
        logger.info("[向量] 索引消息 msg=%s conv=%s role=%s content=%.80s", msg_id, conversation_id, role, content)
    except Exception as e:
        logger.warning("[向量] 索引消息失败 msg=%s: %s", msg_id, e)


def find_relevant_messages(query: str, conversation_id: int, top_k: int = 10) -> list[int]:
    """找与 query 相关的历史消息 id(按相似度排序)。只限同一会话。"""
    if not _available():
        return []
    logger.info("[向量-上下文] find_relevant_messages 入口 query=%.60s conv=%s", query, conversation_id)
    try:
        # 用 get_or_create 而非 get_collection: 集合尚未创建时也能自愈,
        # 避免重置后集合被删、本函数抢先执行而抛 404。
        col = _client().get_or_create_collection(name=CTX_COLLECTION, embedding_function=_ef())
        res = col.query(
            query_texts=[query],
            n_results=min(top_k, 20),
            where={"conversation_id": conversation_id},
        )
        ids_raw = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        relevant = []
        discarded = 0
        for rid, d in zip(ids_raw, dists):
            sim = 1 - d  # 余弦距离 → 相似度
            if sim >= CTX_SIMILARITY_THRESHOLD:
                msg_id = int(rid.replace("msg_", ""))
                relevant.append((msg_id, sim))
            else:
                discarded += 1
        relevant.sort(key=lambda x: x[0])
        result = [r[0] for r in relevant]
        best_sim = max((s for _, s in relevant), default=0.0)
        logger.info(
            "[向量-上下文] 检索结果 query=%.60s conv=%s 命中=%d 最高相似度=%.3f 阈值=%.2f ids=%s (未达标丢弃=%d)",
            query, conversation_id, len(result), best_sim,
            CTX_SIMILARITY_THRESHOLD, result, discarded,
        )
        if not result:
            logger.info("[向量-上下文] 未命中(全部低于阈值 %.2f), 不注入历史消息", CTX_SIMILARITY_THRESHOLD)
        return result
    except Exception as e:
        logger.warning("[向量] 上下文检索失败: %s", e)
        return []


def find_relevant_message_contents(query: str, conversation_id: int,
                                   top_k: int = 6) -> list[dict]:
    """3.3 多轮上下文连贯:返回与本会话相关历史消息的 {content, score} 列表(阈值过滤)。

    与 find_relevant_messages 区别:直接回带消息正文,供 Worker 注入为上下文消息,
    让模型即便前端只发了近期窗口也能看到语义相关的历史片段,实现跨轮连贯。
    """
    if not _available():
        return []
    try:
        col = _client().get_or_create_collection(name=CTX_COLLECTION, embedding_function=_ef())
        res = col.query(
            query_texts=[query],
            n_results=min(top_k, 20),
            where={"conversation_id": conversation_id},
        )
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[dict] = []
        best = 0.0
        for d, s in zip(docs, dists):
            sim = 1 - s
            if sim >= CTX_SIMILARITY_THRESHOLD and d and d.strip():
                out.append({"content": d[:600], "score": round(sim, 3)})
                best = max(best, sim)
        logger.info(
            "[向量-上下文] 内容召回 query=%.60s conv=%s 命中=%d 最高相似度=%.3f 阈值=%.2f",
            query, conversation_id, len(out), best, CTX_SIMILARITY_THRESHOLD,
        )
        if not out:
            logger.info("[向量-上下文] 内容召回未命中(低于阈值 %.2f), 不注入历史消息",
                        CTX_SIMILARITY_THRESHOLD)
        return out
    except Exception as e:
        logger.warning("[向量] 上下文内容召回失败: %s", e)
        return []


# ============================================================
# v0.9.0 新增集合: user_preferences / project_memory /
#               project_code / error_patterns
# ============================================================

# ---- user_preferences (用户偏好, 按 user_id 隔离) ----

def upsert_user_preference(user_id: int, ptype: str, content: str,
                           importance: int = 3, source: str = "distill") -> None:
    """写入/更新一条用户偏好。id 基于 user_id+type+content hash 做幂等。
    ptype: "style"|"constraint"|"habit"
    """
    if not _available() or not content.strip():
        return
    hash4 = _short_hash(content)
    pid = f"upref_{user_id}_{ptype}_{hash4}"
    doc = content[:2000]
    import time
    _upsert(
        settings.chroma_collection_user_preferences,
        ids=[pid], docs=[doc],
        metas=[{"user_id": user_id, "type": ptype, "importance": importance,
                "source": source, "ts": int(time.time())}],
    )
    logger.info("[向量] 用户偏好 upsert user=%s type=%s hash=%s", user_id, ptype, hash4)


def retrieve_user_preferences(user_id: int, query: str, top_k: int = 5) -> list[dict]:
    """检索用户偏好，按 user_id 隔离。"""
    res = _retrieve_where(
        query, settings.chroma_collection_user_preferences,
        where={"user_id": user_id}, top_k=top_k,
    )
    logger.info("[向量-个性化] 检索 user_preferences user=%s query=%.50s 命中=%d 条 top=%s",
                user_id, query, len(res), (res[0]["content"][:80] if res else ""))
    return res


# ---- project_memory (项目记忆, 按 project_id 隔离) ----

def upsert_project_memory(project_id: int, user_id: int, ptype: str,
                          content: str, importance: int = 3) -> None:
    """写入/更新一条项目记忆(决策/约束/需求/产物/事实)。
    ptype: "decision"|"constraint"|"requirement"|"artifact"|"fact"
    """
    if not _available() or not content.strip():
        return
    hash4 = _short_hash(content)
    pid = f"pmem_{project_id}_{ptype}_{hash4}"
    doc = content[:2000]
    import time
    _upsert(
        settings.chroma_collection_project_memory,
        ids=[pid], docs=[doc],
        metas=[{"project_id": project_id, "user_id": user_id, "type": ptype,
                "importance": importance, "ts": int(time.time())}],
    )
    logger.info("[向量] 项目记忆 upsert proj=%s type=%s hash=%s", project_id, ptype, hash4)


def retrieve_project_memory(project_id: int, query: str, top_k: int = 5) -> list[dict]:
    """检索项目记忆，按 project_id 隔离。"""
    res = _retrieve_where(
        query, settings.chroma_collection_project_memory,
        where={"project_id": project_id}, top_k=top_k,
    )
    logger.info("[向量-个性化] 检索 project_memory proj=%s query=%.50s 命中=%d 条 top=%s",
                project_id, query, len(res), (res[0]["content"][:80] if res else ""))
    return res


# ---- project_code (大项目代码语义索引, 按 project_id 隔离, hash 去重) ----

def upsert_project_code(project_id: int, file_path: str, chunk_text: str,
                        chunk_hash: str, function_name: str | None = None,
                        language: str = "html") -> None:
    """写入/更新一个代码块。id 基于 project_id+file_path+chunk_hash 幂等。"""
    if not _available() or not chunk_text.strip():
        return
    pid = f"pcode_{project_id}_{file_path.replace('/', '_')}_{chunk_hash[:8]}"
    doc = chunk_text[:2000]
    import time
    _upsert(
        settings.chroma_collection_project_code,
        ids=[pid], docs=[doc],
        metas=[{"project_id": project_id, "file_path": file_path,
                "function_name": function_name or "", "language": language,
                "chunk_hash": chunk_hash, "ts": int(time.time())}],
    )


def retrieve_project_code(project_id: int, query: str, top_k: int = 8) -> list[dict]:
    """检索项目代码块，按 project_id 隔离。"""
    res = _retrieve_where(
        query, settings.chroma_collection_project_code,
        where={"project_id": project_id}, top_k=top_k,
    )
    logger.info("[向量-代码] 检索 project_code proj=%s query=%.50s 命中=%d 条 top=%s",
                project_id, query, len(res), (res[0]["content"][:80] if res else ""))
    return res


# ---- error_patterns (全局错误模式库, 跨项目共享) ----

def upsert_error_pattern(error_type: str, trigger_pattern: str, fix_pattern: str,
                         language: str = "general") -> None:
    """写入/更新一条错误模式(修复确认后调用)。success_count += 1 在业务层处理。"""
    if not _available():
        return
    hash4 = _short_hash(error_type)
    eid = f"err_{hash4}"
    doc = f"{trigger_pattern} → {fix_pattern}"
    import time
    _upsert(
        settings.chroma_collection_error_patterns,
        ids=[eid], docs=[doc[:2000]],
        metas=[{"error_type": error_type, "trigger_pattern": trigger_pattern,
                "fix_pattern": fix_pattern, "language": language,
                "success_count": 1, "ts": int(time.time())}],
    )
    logger.info("[向量] 错误模式 upsert type=%s hash=%s", error_type, hash4)


def retrieve_error_patterns(query: str, top_k: int = 5) -> list[dict]:
    """检索全局错误模式(无需隔离 where)。"""
    res = retrieve(query, settings.chroma_collection_error_patterns, top_k=top_k)
    logger.info("[向量-错误模式] 检索 error_patterns query=%.50s 命中=%d 条 top=%s",
                query, len(res), (res[0]["content"][:80] if res else ""))
    return res


# ---- 批量种子(错误模式库冷启动) ----

_ERROR_SEEDS: list[dict] = [
    {"error_type": "flex overflow", "trigger": "display:flex 无 min-width:0",
     "fix": "加 min-width:0 或 overflow:hidden 到 flex 子元素", "language": "css"},
    {"error_type": "z-index stacking", "trigger": "z-index 无定位上下文",
     "fix": "确保父元素 position:relative 且非 auto z-index", "language": "css"},
    {"error_type": "grid overflow", "trigger": "grid 列使用 1fr 但内容超宽",
     "fix": "加 minmax(0,1fr) 或 overflow:hidden", "language": "css"},
    {"error_type": "button hover missing", "trigger": "按钮无 :hover/:focus 样式",
     "fix": "添加 hover 变色 + focus-visible 轮廓", "language": "css"},
    {"error_type": "img alt missing", "trigger": "<img> 标签无 alt 属性",
     "fix": "添加描述性 alt 文本", "language": "html"},
    {"error_type": "missing viewport meta", "trigger": "无 <meta name='viewport'>",
     "fix": "加 <meta name='viewport' content='width=device-width,initial-scale=1'>", "language": "html"},
    {"error_type": "semantic heading skip", "trigger": "h1→h3 跳级(无 h2)",
     "fix": "使用顺序标题层级 h1→h2→h3", "language": "html"},
    {"error_type": "color contrast low", "trigger": "浅色文字在浅色背景上(对比度<4.5)",
     "fix": "调深文字色或加深背景,确保 WCAG AA 对比度≥4.5", "language": "css"},
    {"error_type": "responsive breakpoint missing", "trigger": "固定宽度 px 值在移动端溢出",
     "fix": "使用 max-width+百分比 或 @media 断点适配", "language": "css"},
    {"error_type": "CSS variable fallback missing", "trigger": "var(--custom) 无回退值",
     "fix": "加 var(--custom, fallback) 确保旧浏览器兼容", "language": "css"},
    {"error_type": "form label missing", "trigger": "<input> 无关联 <label>",
     "fix": "添加 <label for='id'> 或 aria-label", "language": "html"},
    {"error_type": "nav accessibility", "trigger": "导航无 <nav> 标签或 aria 属性",
     "fix": "用 <nav aria-label='主导航'> 包裹导航链接", "language": "html"},
    {"error_type": "section no heading", "trigger": "<section> 无标题元素",
     "fix": "每个 <section> 包含一个 h2-h6 标题", "language": "html"},
    {"error_type": "hover only interaction", "trigger": "仅 :hover 触发交互无 :focus",
     "fix": "同时添加 :focus 或 :focus-visible 支持键盘导航", "language": "css"},
    {"error_type": "absolute positioning no relative parent", "trigger": "position:absolute 无 position:relative 父元素",
     "fix": "给直接父元素加 position:relative", "language": "css"},
    {"error_type": "font stack no fallback", "trigger": "font-family 只有自定义字体无系统兜底",
     "fix": "加 sans-serif / serif 系统字体兜底", "language": "css"},
    {"error_type": "click target too small", "trigger": "可点击元素 < 44x44px",
     "fix": "设 min-width/min-height:44px 或 padding 扩大点击区", "language": "css"},
    {"error_type": "animation no reduce-motion", "trigger": "动画无 prefers-reduced-motion 适配",
     "fix": "用 @media(prefers-reduced-motion:reduce) 禁用/减弱动画", "language": "css"},
    {"error_type": "link text not descriptive", "trigger": "链接文字为'点击这里''了解更多'",
     "fix": "使用描述性链接文字如'查看产品文档'", "language": "html"},
    {"error_type": "page title missing", "trigger": "<title> 缺失或为空",
     "fix": "每个页面包含描述性 <title>", "language": "html"},
]


def seed_error_patterns() -> int:
    """写入错误模式种子数据(冷启动)。幂等: 同 error_type 覆盖。返回写入条数。"""
    if not _available():
        return 0
    count = 0
    import time
    now = int(time.time())
    for item in _ERROR_SEEDS:
        hash4 = _short_hash(item["error_type"])
        _upsert(
            settings.chroma_collection_error_patterns,
            ids=[f"err_{hash4}"],
            docs=[f"{item['trigger']} → {item['fix']}"],
            metas=[{"error_type": item["error_type"],
                    "trigger_pattern": item["trigger"],
                    "fix_pattern": item["fix"],
                    "language": item["language"],
                    "success_count": 0, "ts": now}],
        )
        count += 1
    logger.info("[向量] 错误模式种子写入 %d 条", count)
    return count


def _seed_bootstrap() -> None:
    """冷启动种子:components / error_patterns 集合为空时填充(幂等,固定 id 覆盖)。

    解决此前 scripts/seed_rag_components.py 缺失导致两集合长期为 0 的问题——
    3.2 组件库 RAG 与 3.4 错误经验库此前始终是空操作,即使读侧接通也检索不到数据。
    """
    if not _available():
        return
    # components
    comp_col = _client().get_or_create_collection(
        name=settings.chroma_collection_components, embedding_function=_ef())
    if comp_col.count() == 0:
        n = seed_components(_COMPONENT_SEEDS)
        logger.info("[seed] components 冷启动写入 %d 条", n)
    else:
        logger.info("[seed] components 已存在 %d 条,跳过", comp_col.count())
    # error_patterns
    err_col = _client().get_or_create_collection(
        name=settings.chroma_collection_error_patterns, embedding_function=_ef())
    if err_col.count() == 0:
        n = seed_error_patterns()
        logger.info("[seed] error_patterns 冷启动写入 %d 条", n)
    else:
        logger.info("[seed] error_patterns 已存在 %d 条,跳过", err_col.count())
