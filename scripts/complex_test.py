"""快速复杂测试：先从需求→建站建立上下文，再跑复杂语句。"""
import asyncio, httpx, json, time, re, sys, os
os.environ.setdefault("PYTHONUNBUFFERED","1")

AI = "http://127.0.0.1:7102"
USER, PASS = "huzhen", "huzhen189"

# 30条复杂测试，涵盖多步/术语/复杂条件/混合
CASES = [
    # 1-5 先建立上下文：需求→建站
    ("建站热身", "做一个包含首页、作品集、关于我三个页面的个人摄影网站，风格极简，主色深蓝"),
    ("建站热身", "生成完整的HTML代码"),
    ("建站热身", "给首页Banner加一个打字机文字动画效果"),
    
    # 4-10 多步骤复杂指令
    ("多步", "先分析一下现有网站的无障碍访问性问题，然后加上aria标签，最后确保颜色对比度符合WCAG AA标准"),
    ("多步", "把CSS改成用CSS Variables管理主题色，然后实现暗色模式切换，暗色模式的配色用深灰背景#1a1a2e配浅色文字#e0e0e0"),
    ("多步", "写一个JavaScript图片轮播组件并在作品集页面使用，要求支持自动播放、左右箭头切换、底部圆点指示器"),
    ("多步", "给导航栏加滚动监听，向下滚动时缩小导航栏高度并加阴影，向上滚动时恢复"),
    
    # 11-18 技术术语混杂
    ("术语", "用CSS Grid的grid-template-areas重新布局作品集页面，每个作品卡片要有hover时的transform scale和box-shadow过渡动画"),
    ("术语", "优化首屏加载性能：图片加loading=lazy，CSS加will-change提示，JS用requestAnimationFrame和Intersection Observer实现滚动触发动画"),
    ("术语", "给联系表单加上客户端验证，用正则校验邮箱格式，用HTML5 Constraint Validation API做必填字段检查，验证失败时显示红色提示文字"),
    ("术语", "实现一个简单的响应式布局系统：用CSS clamp()做流式字体，用container queries做组件级响应，支持320px到2560px的屏幕"),
    ("术语", "用CSS animation的@keyframes做一个呼吸灯效果的按钮，用cubic-bezier缓动函数让动画更自然"),
    ("术语", "把字体加载策略改成font-display:swap并预加载woff2文件，避免FOIT无样式文本闪烁"),
    
    # 19-25 复杂多条件
    ("复杂条件", "在这个单页应用里实现平滑的锚点导航滚动，要求：URL hash更新但不触发页面跳转，滚动到目标位置时标题高亮，滚动行为用scroll-behavior:smooth，并且要兼容Safari"),
    ("复杂条件", "做一个响应式图片方案：用picture元素和srcset提供WebP和JPEG两种格式，用sizes属性配合CSS Grid的minmax实现自适应，所有图片必须有alt文本"),
    ("复杂条件", "给网站增加PWA能力：注册Service Worker实现离线缓存，添加Web App Manifest，配置theme-color和display:standalone，离线时显示自定义的离线页面"),
    ("复杂条件", "实现一个高性能的防抖搜索功能：输入框300ms防抖，搜索结果用虚拟列表渲染（只渲染可见项），支持键盘上下选择，高亮匹配文字"),
    
    # 26-30 综合压轴
    ("混合", "分析一下这个网站目前使用了哪些现代Web技术（语义化HTML标签、CSS变量、Grid布局、Intersection Observer），然后写一份技术栈报告"),
    ("混合", "帮我检查一下这个网站的安全性：是否设置了正确的CSP头，是否有XSS漏洞，表单是否有CSRF保护方案"),
    ("多步", "给网站全部中文化：所有英文文本翻译成中文，meta标签的lang改成zh-CN，Open Graph的locale改成zh_CN"),
    ("复杂条件", "最后做一次全面的性能审计：检查LCP最大内容绘制、CLS布局偏移、FID首次输入延迟，给出优化建议和代码修复，确保Lighthouse评分至少85分"),
    ("混合", "生成一份完整的项目README文档，包含技术栈说明、项目结构、本地运行步骤、部署方案和贡献指南"),
]

async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)) as c:
        # Login at business endpoint
        r = await c.post("http://127.0.0.1:7101/auth/login", json={"username": USER, "password": PASS})
        m = re.search(r"access_token=([^;]+)", r.headers.get("set-cookie", ""))
        token = m.group(1) if m else None
        if not token: print("Login failed"); return
        print("✅ 登录成功")
        
        # Create project + conversation via 7101
        h = {"Cookie": f"access_token={token}"}
        r = await c.post("http://127.0.0.1:7101/api/projects", headers=h, json={"name": "复杂测试"})
        pid = r.json().get("id") if r.status_code in (200, 201) else 1
        r = await c.post("http://127.0.0.1:7101/api/conversations", headers=h, json={"title": "复杂流程测试", "project_id": pid})
        cid = r.json().get("id") if r.status_code in (200, 201) else 1
        print(f"✅ 项目={pid} 对话={cid}")
        
        stats = {"total": 0, "pass": 0, "total_time": 0.0, "total_tok": 0}
        for i, (cat, text) in enumerate(CASES, 1):
            t0 = time.time()
            job = {"model_id": "deepseek", "messages": [{"role": "user", "content": text}],
                   "trace_id": f"cx-{i}-{int(t0*1000)%10000}", "conversation_id": cid,
                   "user_id": 1, "project_id": pid}
            ce = None; dp = []; events = []; tok = 0; done = False; err = False; qc = False
            try:
                async with c.stream("POST", f"{AI}/generate", json=job) as resp:
                    async for line in resp.aiter_lines():
                        if line == "":
                            if ce or dp:
                                d = "".join(dp); events.append(ce)
                                if ce == "done": done = True
                                elif ce == "token":
                                    try: 
                                        obj = json.loads(d)
                                        tok += len(obj.get("data", ""))
                                    except: pass
                                elif ce == "qc": qc = True
                                elif ce == "error": err = True
                                ce = None; dp = []
                        elif line.startswith("event: "): ce = line[7:].strip()
                        elif line.startswith("data: "): dp.append(line[6:])
            except Exception as e:
                err = True
            elapsed = time.time() - t0
            ok = done and not err
            if ok: stats["pass"] += 1
            stats["total"] += 1
            stats["total_time"] += elapsed
            stats["total_tok"] += tok
            s = "✅" if ok else "❌"
            print(f"  [{i:02d}] {s} [{cat}] {text[:35]:35s} | ev={len(events):4d} tok={tok:5d} {'done' if done else 'err' if err else '?'} qc={qc} {elapsed:.1f}s")
        
        rate = stats["pass"] / stats["total"] * 100
        print(f"\n{'='*60}")
        print(f"通过率: {rate:.0f}% ({stats['pass']}/{stats['total']})")
        print(f"总耗时: {stats['total_time']:.0f}s | 总token: {stats['total_tok']}")
        print(f"{'='*60}")

asyncio.run(main())
