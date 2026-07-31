"""共享层配置导出。

配置单一真相源已经迁入 ``app.config``。共享产物模块在迁移完成前通过此处
读取同一个对象；此文件不声明默认值、不解析环境变量，也不会产生第二份配置。
"""

from app.config import ENV_FILE, runtime_config, settings


__all__ = ["ENV_FILE", "runtime_config", "settings"]
