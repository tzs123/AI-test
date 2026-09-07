package service

import (
	"context"
	"errors"

	"github.com/runnergo/runnergo/backend/internal/agent"
)

// AgentService is the application-facing layer. It keeps authorization and
// orchestration concerns out of the reusable agent package.
type AgentService struct {
	Engine       *agent.AgentEngine
	Orchestrator *agent.Orchestrator
}

func NewAgentService(engine *agent.AgentEngine) *AgentService {
	if engine == nil {
		engine = agent.NewAgentEngine(nil, nil, nil, nil)
	}
	return &AgentService{Engine: engine, Orchestrator: agent.NewOrchestrator(engine)}
}

func (s *AgentService) Generate(ctx context.Context, userID uint, task agent.TestTask) ([]agent.TestCase, error) {
	if userID == 0 {
		return nil, errors.New("authenticated user is required")
	}
	if ctx != nil {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
		}
	}
	return s.Engine.GenerateTestCases(task)
}
func (s *AgentService) Execute(ctx context.Context, userID uint, cases []agent.TestCase) ([]agent.TestResult, error) {
	if userID == 0 {
		return nil, errors.New("authenticated user is required")
	}
	return s.Engine.ExecuteTestCases(cases)
}
func (s *AgentService) Run(ctx context.Context, userID uint, task agent.TestTask) (agent.OrchestrationResult, error) {
	if userID == 0 {
		return agent.OrchestrationResult{}, errors.New("authenticated user is required")
	}
	return s.Orchestrator.Run(ctx, task)
}
