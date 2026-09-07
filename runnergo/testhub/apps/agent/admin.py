from django.contrib import admin

from .models import (
    AgentExecutionLog,
    AgentMemory,
    AgentStep,
    AgentTask,
    E2EExecutionLog,
    E2ETask,
    TestAsset,
    TestExecutionResult,
)


admin.site.register(AgentTask)
admin.site.register(AgentStep)
admin.site.register(AgentMemory)
admin.site.register(AgentExecutionLog)
admin.site.register(TestAsset)
admin.site.register(TestExecutionResult)
admin.site.register(E2ETask)
admin.site.register(E2EExecutionLog)
