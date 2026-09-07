package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
)

type Agent interface {
	Name() string
	Role() string
	Process(task Task) Result
}

type DesignAgent struct{ Engine *AgentEngine }

func (a *DesignAgent) Name() string { return "design-agent" }
func (a *DesignAgent) Role() string { return "测试设计Agent" }
func (a *DesignAgent) Process(task Task) Result {
	if a == nil || a.Engine == nil {
		return Result{Status: "failed", Error: "design agent engine is nil"}
	}
	cases, err := a.Engine.GenerateTestCases(task)
	if err != nil && len(cases) == 0 {
		return Result{Status: "failed", Error: err.Error()}
	}
	return Result{Status: "success", Data: cases, Metadata: map[string]interface{}{"warning": errorString(err)}}
}

type ExecuteAgent struct{ Engine *AgentEngine }

func (a *ExecuteAgent) Name() string { return "execute-agent" }
func (a *ExecuteAgent) Role() string { return "执行Agent" }
func (a *ExecuteAgent) Process(task Task) Result {
	if a == nil || a.Engine == nil {
		return Result{Status: "failed", Error: "execute agent engine is nil"}
	}
	var cases []TestCase
	if raw, ok := task.Context["test_cases"]; ok {
		b, _ := json.Marshal(raw)
		_ = json.Unmarshal(b, &cases)
	}
	if len(cases) == 0 {
		return Result{Status: "failed", Error: "task has no test cases"}
	}
	results, err := a.Engine.ExecuteTestCasesForProject(task.ProjectID, cases)
	if err != nil {
		return Result{Status: "failed", Data: results, Error: err.Error()}
	}
	return Result{Status: "success", Data: results}
}

type EvaluateAgent struct{ Engine *AgentEngine }

func (a *EvaluateAgent) Name() string { return "evaluate-agent" }
func (a *EvaluateAgent) Role() string { return "评估Agent" }
func (a *EvaluateAgent) Process(task Task) Result {
	if a == nil || a.Engine == nil {
		return Result{Status: "failed", Error: "evaluate agent engine is nil"}
	}
	var results []TestResult
	if raw, ok := task.Context["results"]; ok {
		b, _ := json.Marshal(raw)
		_ = json.Unmarshal(b, &results)
	}
	report := a.Engine.EvaluateQuality(results)
	return Result{Status: "success", Data: report}
}

type Orchestrator struct {
	Engine *AgentEngine
	Agents []Agent
}

func NewOrchestrator(engine *AgentEngine, agents ...Agent) *Orchestrator {
	if len(agents) == 0 {
		agents = []Agent{&DesignAgent{engine}, &ExecuteAgent{engine}, &EvaluateAgent{engine}}
	}
	return &Orchestrator{Engine: engine, Agents: agents}
}
func (o *Orchestrator) RegisterAgent(a Agent) {
	if o != nil && a != nil {
		o.Agents = append(o.Agents, a)
	}
}
func (o *Orchestrator) Process(task Task) Result {
	result, err := o.Run(context.Background(), task)
	if err != nil {
		return Result{Status: "failed", Data: result, Error: err.Error()}
	}
	return Result{Status: "success", Data: result}
}
func (o *Orchestrator) Run(ctx context.Context, task TestTask) (OrchestrationResult, error) {
	if o == nil || o.Engine == nil {
		return OrchestrationResult{}, fmt.Errorf("orchestrator engine is nil")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	result := OrchestrationResult{Task: task}
	if len(o.Agents) < 3 {
		return result, fmt.Errorf("orchestrator requires design, execute and evaluate agents")
	}
	if err := ctx.Err(); err != nil {
		return result, err
	}
	design := o.Agents[0].Process(task)
	result.Steps = append(result.Steps, o.Agents[0].Name())
	if design.Status != "success" {
		return result, fmt.Errorf("%s: %s", o.Agents[0].Name(), design.Error)
	}
	cases, ok := design.Data.([]TestCase)
	if !ok {
		return result, fmt.Errorf("design agent returned invalid data")
	}
	result.TestCases = cases
	task.Context = cloneContext(task.Context)
	task.Context["test_cases"] = cases
	execResult := o.Agents[1].Process(task)
	result.Steps = append(result.Steps, o.Agents[1].Name())
	if execResult.Status != "success" && execResult.Data == nil {
		return result, fmt.Errorf("%s: %s", o.Agents[1].Name(), execResult.Error)
	}
	raw, _ := json.Marshal(execResult.Data)
	_ = json.Unmarshal(raw, &result.Results)
	task.Context["results"] = result.Results
	eval := o.Agents[2].Process(task)
	result.Steps = append(result.Steps, o.Agents[2].Name())
	if eval.Status != "success" {
		return result, fmt.Errorf("%s: %s", o.Agents[2].Name(), eval.Error)
	}
	if report, ok := eval.Data.(QualityReport); ok {
		result.Quality = report
	}
	return result, nil
}
func cloneContext(in map[string]interface{}) map[string]interface{} {
	out := map[string]interface{}{}
	for k, v := range in {
		out[k] = v
	}
	return out
}
func errorString(err error) string {
	if err == nil {
		return ""
	}
	return strings.TrimSpace(err.Error())
}
