"""AI 测试任务状态机。"""

# 任务状态（用户需求）
CREATED = "CREATED"                       # 已创建，等待规划
ANALYZING = "ANALYZING"                   # 分析需求 / 生成测试计划
GENERATING_DATA = "GENERATING_DATA"       # 创建测试数据
GENERATING_CASE = "GENERATING_CASE"       # 生成测试用例
RUNNING = "RUNNING"                       # 执行测试
FAILURE_ANALYZING = "ANALYZING"           # AI 分析失败原因（与 ANALYZING 同值，按阶段区分）
COMPLETED = "COMPLETED"                   # 完成
FAILED = "FAILED"                         # 失败
STOPPED = "STOPPED"                       # 已停止

WORKFLOW_STATUSES = [
    CREATED,
    ANALYZING,
    GENERATING_DATA,
    GENERATING_CASE,
    RUNNING,
    FAILURE_ANALYZING,
    COMPLETED,
    FAILED,
    STOPPED,
]

WORKFLOW_STATUS_TEXT = {
    CREATED: "已创建",
    ANALYZING: "分析需求",
    GENERATING_DATA: "创建测试数据",
    GENERATING_CASE: "生成测试用例",
    RUNNING: "执行测试",
    FAILURE_ANALYZING: "AI 分析失败原因",
    COMPLETED: "已完成",
    FAILED: "失败",
    STOPPED: "已停止",
}

# 工作流阶段（前端 Timeline 展示顺序）
WORKFLOW_PHASES = [
    {"phase": "analyze", "label": "分析需求", "icon": "🔍"},
    {"phase": "plan", "label": "生成测试计划", "icon": "📋"},
    {"phase": "data", "label": "创建测试数据", "icon": "🗄️"},
    {"phase": "case", "label": "生成测试用例", "icon": "🧪"},
    {"phase": "run", "label": "执行测试", "icon": "⚡"},
    {"phase": "analysis", "label": "AI 分析失败原因", "icon": "🧠"},
    {"phase": "report", "label": "生成报告", "icon": "📊"},
]

# 步骤状态
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCESS = "success"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

STEP_STATUSES = [STEP_PENDING, STEP_RUNNING, STEP_SUCCESS, STEP_FAILED, STEP_SKIPPED]

STEP_STATUS_TEXT = {
    STEP_PENDING: "等待中",
    STEP_RUNNING: "执行中",
    STEP_SUCCESS: "成功",
    STEP_FAILED: "失败",
    STEP_SKIPPED: "跳过",
}

# 阶段 -> 工作流状态
PHASE_TO_STATUS = {
    "analyze": ANALYZING,
    "plan": ANALYZING,
    "data": GENERATING_DATA,
    "case": GENERATING_CASE,
    "run": RUNNING,
    "analysis": FAILURE_ANALYZING,
    "report": COMPLETED,
}

TERMINAL_WORKFLOW_STATUSES = {COMPLETED, FAILED, STOPPED}
