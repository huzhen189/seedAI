# 多意图 e2e 测试报告 (site + weather)

查询: 帮我做一个个人摄影作品集网站，包含首页和关于页两个页面，现代简约风格，主色用深蓝；另外帮我查一下今天深圳的天气怎么样？

- [OK] 注册成功 (http=200, user=e2e_mi_1785209732)
- [OK] 建项目成功 (pid=19)
- [OK] 建对话成功 (cid=19)
- [OK] 首跑返回 await_confirm(paused) (events=52, intents=[], done=True)
- [OK] 首跑未 error (err=None)
- [OK] 续跑完成 done (events=31189, elapsed=1106.6s)
- [OK] 续跑无 error (err=None)
- [OK] 多意图拆分(双子任务均执行: 网站预览+天气文本) (preview=https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com/previews/20/anon/v1/index.html, weather_hit=True, intents=[('build', 'site')])
- [OK] 网站预览产物(preview) (preview=https://seedhtml-1252059540.cos.ap-guangzhou.myqcloud.com/previews/20/anon/v1/index.html)
- [OK] 天气文本产物(深圳/天气/℃) (weather_hit=True, tokens=119219)
- [OK] 续跑未再重弹 paused(断点续跑) (paused=False)

**结果: 11/11 通过**
