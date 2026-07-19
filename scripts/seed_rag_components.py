"""将优质模板片段批量写入 Chroma `components` 集合(②-a 数据准备)。

用法:
  python scripts/seed_rag_components.py path/to/components.json

components.json 格式(每项一个组件 / 模板片段):
  [
    {
      "content": "一个英雄区(hero)的 HTML/CSS 片段或完整描述...",
      "metadata": {"title": "Hero 首页横幅", "tags": "landing,hero,responsive"}
    },
    {
      "content": "一个导航栏的 HTML/CSS 片段或完整描述...",
      "metadata": {"title": "导航栏(顶栏)", "tags": "nav,header,sticky"}
    }
  ]

content 可以是 HTML 代码片段、CSS 类名组合描述、或自然语言设计说明——无所谓,
Chroma 存入的是纯文本,检索时靠语义相似度匹配。metadata 中 title/tags 会并存,
便于后续筛选或调试。

前置条件:
  - 项目根 .env 已配置 QWEN_EMBEDDING_KEY 与 CHROMA_URL。
  - pip install chromadb(已在 ai_service/requirements.txt)。
  - Chroma 服务可达(本地 :8000 或云地址)。

注意:重复运行会覆盖同 id 的旧数据(upsert),不会产生重复行。
"""

import json
import sys
from pathlib import Path


# 添加 ai_service 到路径,以便 import app.rag
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "ai_service"))

from app.rag import seed_components  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/seed_rag_components.py path/to/components.json")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"文件不存在: {path}")
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        print("错误:JSON 顶层应为数组 [{content, metadata}, ...]")
        sys.exit(1)

    count = seed_components(data)
    print(f"已写入 {count} 条组件到 Chroma `components` 集合。")


if __name__ == "__main__":
    main()
