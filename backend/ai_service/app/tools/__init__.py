"""Tools 包:导入即注册(§5.9 来源 A 内置工具)。

每个模块用 @tool 装饰器,在导入时把函数注册进 ToolRegistry。
新增内置工具:在此文件加一行 `from . import xxx` 即可(开闭原则)。
重依赖(chromadb/cos/playwright)均函数内懒加载,缺包也不影响包导入与注册。
"""
from . import (
    file_io,
    fetch_url,
    web_search,
    html_validate,
    rag_retrieve,
    cos_upload,
    browser_screenshot,
    image_generate,
)

__all__ = [
    "file_io",
    "fetch_url",
    "web_search",
    "html_validate",
    "rag_retrieve",
    "cos_upload",
    "browser_screenshot",
    "image_generate",
]
