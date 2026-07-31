"""执行层配置导出。

新代码统一直接导入 ``app.config``；该模块只保证尚未迁移完成的执行层模块
在同一重构提交序列中读取同一 ``Settings`` 实例，不再定义任何独立配置。
"""

from app.config import runtime_config, settings


__all__ = ["runtime_config", "settings"]
