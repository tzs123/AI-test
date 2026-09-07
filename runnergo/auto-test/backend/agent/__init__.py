"""AI Test Agent 工作流引擎。

包结构：
    legacy.py   历史 Web UI Agent 编排（兼容旧 /api/agent/* 接口）
    state.py    任务状态机
    memory.py   任务记忆
    tools.py    工具注册表
    planner.py  测试规划器
    analyzer.py 失败分析器
    executor.py 工作流执行器

legacy 模块的全部命名空间被合并进包，保证旧调用
（agent.create_agent_task / agent.project_service / agent._goal_terms 等）完全兼容。
"""

import types

from backend.agent import legacy as _legacy_module

for _name, _value in vars(_legacy_module).items():
    if not _name.startswith("__"):
        globals()[_name] = _value

# 把 legacy 的函数按“包命名空间”重绑定，使 build_test_plan 等函数的
# __globals__ 指向本包。这样外部对 agent._call_model / agent._collect_case_assets
# 的 monkeypatch 与旧版单文件模块语义完全一致。
for _name, _value in list(globals().items()):
    if (
        isinstance(_value, types.FunctionType)
        and _value.__module__ == "backend.agent.legacy"
    ):
        _rebound = types.FunctionType(
            _value.__code__,
            globals(),
            _name,
            _value.__defaults__,
            _value.__closure__,
        )
        _rebound.__kwdefaults__ = _value.__kwdefaults__
        globals()[_name] = _rebound
del _name, _value, types
from backend.agent.state import (  # noqa: F401
    STEP_STATUSES,
    STEP_STATUS_TEXT,
    WORKFLOW_PHASES,
    WORKFLOW_STATUS_TEXT,
    WORKFLOW_STATUSES,
)
from backend.agent.memory import (  # noqa: F401
    clear_memory,
    list_memory,
    recall,
    remember,
)
from backend.agent.tools import (  # noqa: F401
    TOOLS,
    generate_test_data,
    get_test_asset,
    list_test_assets,
    list_tools,
    run_tool,
)
from backend.agent.planner import (  # noqa: F401
    build_test_dimension_plan,
    build_workflow_plan,
    decompose_requirement,
)
from backend.agent.analyzer import (  # noqa: F401
    analyze_failure,
    analyze_task_failures,
)
from backend.agent.executor import (  # noqa: F401
    create_agent_workflow_task,
    delete_workflow_task,
    get_workflow_task,
    get_workflow_timeline,
    list_workflow_tasks,
    recover_workflow_tasks,
    start_workflow_task,
    stop_workflow_task,
    update_workflow_task_status,
)


# AI Agent 核心（ReAct）：统一工具注册表会包含旧工具，直接覆盖 list_tools 提升兼容性
from backend.agent.core.tool_registry import list_tools  # noqa: F401
from backend.agent.core.executor import (  # noqa: F401
    append_execution_log,
    get_execution_log,
    start_react_run,
    stop_react_run,
)
from backend.agent.core.memory import (  # noqa: F401
    list_project_memory,
    recall_project,
    remember_project,
    remember_system_fact,
)

# 保持旧调用兼容：agent.executor 指向平台执行器（backend.executor），
# 新工作流执行器仍可通过 backend.agent.executor 访问。
from backend import executor as executor  # noqa: F401
