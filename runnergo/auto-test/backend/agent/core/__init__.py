"""AI Agent 核心：ReAct（Thought -> Action -> Observation -> Reflection）决策循环。

包结构：
    planner.py          ReAct 规划（复用 agent.planner，补充安全/浏览器步骤）
    decision_engine.py  决策引擎：根据任务状态决定下一个 Action
    executor.py         ReAct 执行器：Thought/Action/Observation/Reflection 循环
    observer.py         观察器：解析工具结果，提取事实
    reflection.py       反思器：判断是否需要调整策略/重试
    memory.py           核心记忆：任务级 + 项目级记忆
    tool_registry.py    统一 Tool 接口与工具注册表

该核心不替代旧版 v2 工作流（agent/executor.py），而是提供可独立调用的
自主决策引擎，并可与 v2 任务关联执行。
"""
from __future__ import annotations

from backend.agent.core.tool_registry import (  # noqa: F401
    Tool,
    execute_tool,
    get_tool,
    list_tools,
    register_tool,
)
from backend.agent.core.observer import observe  # noqa: F401
from backend.agent.core.reflection import reflect  # noqa: F401
from backend.agent.core.decision_engine import (  # noqa: F401
    build_action_queue,
    decide_next_action,
)
from backend.agent.core.executor import (  # noqa: F401
    append_execution_log,
    get_execution_log,
    run_react_workflow,
    start_react_run,
    stop_react_run,
)
from backend.agent.core.memory import (  # noqa: F401
    list_project_memory,
    recall_project,
    remember_project,
    remember_system_fact,
)
