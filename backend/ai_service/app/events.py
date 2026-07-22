"""SSE 事件协议(§3.7 / §5.5)。

事件类型:
- node      : agent 节点进入/离开(stage 字段标明阶段)
- think     : 各 agent 的思考/推理文本(content);分步实时反馈(每一步精准反馈)
- plan      : 大的计划 / 目标特殊节点(title/goal/steps)——前端渲染为「计划/流程」卡片,
              区别于普通 think 文本(如 Planner 产出的结构化需求规格)
- token     : Coder 产出的 HTML 字符流(data 为字符串,进预览)
- error     : 错误(随后结束)
- done      : 结束(随后断连)
- aborted   : 主动取消确认(C1)
- degraded  : 模型降级标记(2-C)
- preview   : 预览直链就绪(url + fallback,供前端打开iframe预览);取代 node(stage=preview)
- orchestration: 多意图编排开始(总览:total/tasks[{id,goal,skill,risk,status}]/strategy);sub_task_id 贯穿后续事件
- subtask_start: 单个子任务开始(sub_task_id/goal/skill)
- subtask_done : 单个子任务完成(sub_task_id/result_summary)
- subtask_fail : 单个子任务失败(sub_task_id/reason/recoverable)
- merge     : 结果合并(sub_task_id贯穿的多个子任务结果 → 一段连贯中文回复);含 success_count/fail_count/failed_tasks
- retry     : 主模型不可用,携带可选替代模型列表(failed/suggested),前端弹框待用户选择后重发
              (替代原自动降级;收到后同 done/error 一样结束 SSE,由前端重新发起请求)

每个事件是一个 dict: {"event": <type>, "data": <payload>};data 可为字符串或 dict,
经 to_sse() 序列化为 SSE 的 {event, data} 字符串帧。
"""

from __future__ import annotations

import json
from typing import Any, Dict


# 终止事件:收到即结束 SSE 流
TERMINAL_EVENTS = {"done", "error", "aborted", "retry"}


def ev(event: str, **data) -> Dict[str, Any]:
    """构造一个事件字典。其余关键字参数作为 data 载荷。"""
    return {"event": event, "data": data}


def to_sse(event_dict: Dict[str, Any]) -> Dict[str, str]:
    """转换为 sse-starlette 所需的 {event, data} 字符串格式(自动 JSON 序列化 data)。"""
    event = event_dict.get("event", "message")
    data = event_dict.get("data", "")
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False)
    elif not isinstance(data, str):
        data = str(data)
    return {"event": event, "data": data}
