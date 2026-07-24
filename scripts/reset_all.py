"""一键重置: 清空数据库 + Redis + Chroma + 重建表 + 自动创建默认超管。

用法(项目根目录):  python scripts/reset_all.py

执行后:
  1. DROP 所有业务表
  2. FLUSHDB 清空 Redis
  3. 清空 Chroma **运行数据**集合(用户/项目运行时数据), **保留配置/知识类集合**(规则/意图/组件库等)
  3.5 清空项目内全部 *.log 日志文件(运行日志, 重置时不保留), 含 logs/ 下的运行时观测 jsonl(如 intent_observations.jsonl)
  4. 重建表 + 补齐缺失列
  5. 自动创建默认超管用户: huzhen / huzhen189 / 超级管理员
  6. 提示重启两个后端服务
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "business"))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from sqlalchemy import text  # noqa: E402


async def reset() -> None:
    print(f"数据库: {settings.database_url[:80]}...")

    # 1) DROP 所有表
    async with engine.begin() as conn:
        def _drop(sync_conn):
            from sqlalchemy import inspect
            insp = inspect(sync_conn)
            tables = insp.get_table_names()
            for t in tables:
                sync_conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
            return tables
        tables = await conn.run_sync(_drop)
        if tables:
            print(f"  >> 已 DROP {len(tables)} 张表: {', '.join(tables)}")
        else:
            print("  >> 无表需清理")

    # 2) 清空 Redis
    try:
        import redis.asyncio as aioredis  # noqa: E402
    except ImportError:
        print("  >> 跳过 Redis(未安装 redis 库)")
    else:
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True, protocol=2)
            await r.flushdb()
            print("  >> Redis 已清空")
            await r.aclose()
        except Exception as e:
            print(f"  >> Redis 清理失败: {e}")

    # 2.5) 清空 Chroma 运行数据集合, 保留配置/知识类集合(规则/意图/组件库等)
    #      集合名与 backend/ai_service/app/config.py 保持一致。
    #      - 配置/知识集合(保留): components(组件库) / error_patterns(错误模式库) / intents(意图向量索引)
    #      - 运行数据集合(清空): memory / cache_gen / user_preferences / project_memory / project_code
    #      说明: 规则 JSON 文件(intent_catalog.json / rules_catalog.json / ruleset.json)在磁盘上,
    #           本脚本不触碰, 天然安全; 此处仅针对 Chroma 内"由规则派生的向量索引"做保留。
    try:
        from urllib.parse import urlparse as _up
        import chromadb
        chroma_url = getattr(settings, 'chroma_url', None) or "http://chroma:8000"
        p = _up(chroma_url)
        c = chromadb.HttpClient(host=p.hostname or "localhost", port=p.port or 8000)

        # 配置/知识类集合 —— 重置时**保留**, 不删除(规则等配置项)
        CHROMA_CONFIG_COLLECTIONS = {"components", "error_patterns", "intents"}
        # 运行数据集合 —— 用户/项目运行时数据, 重置时**清空**
        CHROMA_RUNTIME_COLLECTIONS = {
            "memory", "cache_gen", "user_preferences", "project_memory", "project_code",
        }

        colls = c.list_collections()
        kept, cleared = 0, 0
        for col in colls:
            name = col.name if hasattr(col, "name") else str(col)
            if name in CHROMA_CONFIG_COLLECTIONS:
                print(f"  >> 保留配置集合(不删): {name}")
                kept += 1
            elif name in CHROMA_RUNTIME_COLLECTIONS:
                try:
                    c.delete_collection(name)
                    print(f"  >> 已清空运行数据集合: {name}")
                    cleared += 1
                except Exception as e:
                    print(f"  >> 清空集合失败 {name}: {e}")
            else:
                # 未知集合: 安全起见保留并告警, 避免误删配置
                print(f"  >> 未知集合, 安全保留并告警: {name}")
                kept += 1
        print(f"  >> Chroma 处理完成: 清空 {cleared} 个运行数据集合, 保留 {kept} 个配置/未知集合")
    except Exception as e:
        print(f"  >> Chroma 清理失败(可忽略): {e}")

    # 2.6) 清空项目内全部 *.log 日志文件(运行日志, 重置时不保留)
    #      含 logs/ 目录下的运行时观测 jsonl(如 intent_observations.jsonl); 排除 .git / node_modules。
    #      配置/源代码(JSON/YAML/py 等)一律不动。
    try:
        from pathlib import Path as _P

        _log_deleted = 0
        for logf in ROOT.rglob("*.log"):
            if ".git" in logf.parts or "node_modules" in logf.parts:
                continue
            try:
                logf.unlink()
                _log_deleted += 1
            except Exception:
                pass
        # 运行时观测日志(jsonl): 仅清 logs/ 目录下的, 避免误删源码/数据 json
        for logf in ROOT.rglob("*.jsonl"):
            if ".git" in logf.parts or "node_modules" in logf.parts:
                continue
            if "logs" in logf.parts:  # 仅 logs/ 下的运行时观测日志
                try:
                    logf.unlink()
                    _log_deleted += 1
                except Exception:
                    pass
        print(f"  >> 已清空 {_log_deleted} 个日志文件(*.log + logs/ 下 *.jsonl)")
    except Exception as e:
        print(f"  >> 日志清理失败(可忽略): {e}")

    # 3) 重建表
    from app.models import Base  # noqa: E402
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("  >> 表已重建")

    # 4) 补齐缺失列 + 自动创建默认用户
    await init_db()
    print("  >> 默认用户已就绪")

    # 5) 大表 HASH 分区(幂等, 已分区则跳过)
    _PARTITIONS = {
        "messages": ("conversation_id", 16),
        "traces": ("user_id", 16),
        "trace_events": ("trace_id", 16),
        "feedbacks": ("user_id", 16),
        "usage_logs": ("user_id", 16),
        "artifacts": ("project_id", 8),
    }
    async with engine.begin() as conn:
        def _do_partitions(sync_conn):
            for tbl, (col, n) in _PARTITIONS.items():
                try:
                    sync_conn.execute(text(
                        f"ALTER TABLE {tbl} PARTITION BY HASH({col}) PARTITIONS {n}"
                    ))
                    print(f"  >> {tbl} HASH({col}) {n} 分区已应用")
                except Exception:
                    pass  # 已分区则跳过
        await conn.run_sync(_do_partitions)

    await engine.dispose()
    print("\n完成。业务服务 7101 将自动重启; AI 服务 7102 请手动重启。")


if __name__ == "__main__":
    confirm = input("⚠ 将清空全部数据并重建,确认? [y/N] ")
    if confirm.lower() != "y":
        print("已取消")
        sys.exit(0)
    asyncio.run(reset())
