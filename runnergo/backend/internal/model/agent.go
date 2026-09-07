package model

import "github.com/runnergo/runnergo/backend/internal/agent"

// Aliases keep the conventional RunnerGo model package available to existing
// services while the runtime agent package owns its persistence definitions.
type AgentTask = agent.AgentTaskModel
type AgentTestCase = agent.AgentTestCaseModel
type AgentTestResult = agent.AgentTestResultModel
type KnowledgeDocument = agent.KnowledgeDocumentModel
type KnowledgeChunk = agent.KnowledgeChunkModel
type AgentMemory = agent.AgentMemoryModel
type AgentExecutorConfig = agent.AgentExecutorConfigModel
