package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// LLMClient is intentionally an open interface. Adapters may implement
// Generate, Complete, Chat, or their context-free counterparts; callLLM
// normalizes those variants and keeps the engine provider agnostic.
type LLMClient interface{}

// TestTask is the input accepted by the engine and orchestrator.
type TestTask struct {
	ID          string                 `json:"id"`
	Name        string                 `json:"name"`
	Description string                 `json:"description"`
	ProjectID   uint                   `json:"project_id,omitempty"`
	Context     map[string]interface{} `json:"context,omitempty"`
	Metadata    map[string]string      `json:"metadata,omitempty"`
}

// Task is the common multi-agent task contract.
type Task = TestTask

type TestStep struct {
	Action string `json:"action"`
	Data   string `json:"data,omitempty"`
}

type TestCase struct {
	ID             string                 `json:"id"`
	Title          string                 `json:"title"`
	Description    string                 `json:"description,omitempty"`
	ScenarioType   string                 `json:"scenario_type"` // positive, boundary, negative
	Priority       string                 `json:"priority,omitempty"`
	Preconditions  []string               `json:"preconditions,omitempty"`
	Steps          []TestStep             `json:"steps"`
	ExpectedResult string                 `json:"expected_result"`
	Script         string                 `json:"script,omitempty"`
	Tags           []string               `json:"tags,omitempty"`
	Metadata       map[string]interface{} `json:"metadata,omitempty"`
}

// UnmarshalJSON tolerates common LLM variations (a precondition object or a
// single step string) while retaining the stable public TestCase schema.
func (tc *TestCase) UnmarshalJSON(data []byte) error {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	readString := func(key string, dst *string) {
		if v, ok := raw[key]; ok {
			_ = json.Unmarshal(v, dst)
		}
	}
	readString("id", &tc.ID)
	readString("title", &tc.Title)
	readString("description", &tc.Description)
	readString("scenario_type", &tc.ScenarioType)
	readString("priority", &tc.Priority)
	readString("expected_result", &tc.ExpectedResult)
	readString("script", &tc.Script)
	if v, ok := raw["preconditions"]; ok {
		tc.Preconditions = parseStringList(v)
	}
	if v, ok := raw["tags"]; ok {
		tc.Tags = parseStringList(v)
	}
	if v, ok := raw["steps"]; ok {
		tc.Steps = parseSteps(v)
	}
	if v, ok := raw["metadata"]; ok {
		_ = json.Unmarshal(v, &tc.Metadata)
	}
	return nil
}
func parseStringList(raw json.RawMessage) []string {
	var list []string
	if json.Unmarshal(raw, &list) == nil {
		return list
	}
	var single string
	if json.Unmarshal(raw, &single) == nil {
		return []string{single}
	}
	var objects []interface{}
	if json.Unmarshal(raw, &objects) == nil {
		for _, item := range objects {
			if s, ok := item.(string); ok {
				list = append(list, s)
			} else {
				b, _ := json.Marshal(item)
				list = append(list, string(b))
			}
		}
		return list
	}
	var object map[string]interface{}
	if json.Unmarshal(raw, &object) == nil {
		for _, key := range []string{"value", "content", "rule", "text", "description"} {
			if s, ok := object[key].(string); ok {
				return []string{s}
			}
		}
		b, _ := json.Marshal(object)
		return []string{string(b)}
	}
	return []string{fmt.Sprint(string(raw))}
}
func parseSteps(raw json.RawMessage) []TestStep {
	var steps []TestStep
	if json.Unmarshal(raw, &steps) == nil {
		return steps
	}
	var stringsList []string
	if json.Unmarshal(raw, &stringsList) == nil {
		for _, s := range stringsList {
			steps = append(steps, TestStep{Action: s})
		}
		return steps
	}
	var single string
	if json.Unmarshal(raw, &single) == nil {
		return []TestStep{{Action: single}}
	}
	return steps
}

type ExecutionOutput struct {
	Actual string                 `json:"actual"`
	Logs   string                 `json:"logs,omitempty"`
	Data   map[string]interface{} `json:"data,omitempty"`
	// Passed lets protocol-aware executors (for example HTTP status assertions)
	// provide an explicit assertion result. A nil value keeps the historical
	// exact-string comparison behaviour used by command executors.
	Passed *bool `json:"-"`
}

// ExecutorConfig is the persisted runtime configuration for the test-case
// executor. Type "api" sends HTTP requests, "command" runs an allow-listed
// binary, and "none" disables execution (cases are reported as skipped).
type ExecutorConfig struct {
	ID              uint              `json:"id,omitempty"`
	ProjectID       uint              `json:"project_id,omitempty"`
	Enabled         bool              `json:"enabled"`
	Type            string            `json:"type"` // api, command, none
	BaseURL         string            `json:"base_url,omitempty"`
	Method          string            `json:"method,omitempty"`
	ExpectedStatus  int               `json:"expected_status,omitempty"`
	TimeoutMS       int64             `json:"timeout_ms,omitempty"`
	Headers         map[string]string `json:"headers,omitempty"`
	Token           string            `json:"token,omitempty"`
	Command         string            `json:"command,omitempty"`
	AllowedCommands []string          `json:"allowed_commands,omitempty"`
	CreatedBy       uint              `json:"created_by,omitempty"`
	UpdatedAt       time.Time         `json:"updated_at,omitempty"`
}

// ExecutorConfigStore persists executor settings. Implementations may use
// GORM, while tests and standalone deployments can use the in-memory store.
type ExecutorConfigStore interface {
	Load(ctx context.Context, projectID uint) (ExecutorConfig, error)
	Save(ctx context.Context, config ExecutorConfig) (ExecutorConfig, error)
}

type TestResult struct {
	CaseID     string        `json:"case_id"`
	Title      string        `json:"title,omitempty"`
	Status     string        `json:"status"` // passed, failed, skipped, error
	Expected   string        `json:"expected,omitempty"`
	Actual     string        `json:"actual,omitempty"`
	Logs       string        `json:"logs,omitempty"`
	Error      string        `json:"error,omitempty"`
	Duration   time.Duration `json:"duration"`
	StartedAt  time.Time     `json:"started_at"`
	FinishedAt time.Time     `json:"finished_at"`
}

type QualityReport struct {
	TotalCases          int      `json:"total_cases"`
	ExecutedCases       int      `json:"executed_cases"`
	PassedCases         int      `json:"passed_cases"`
	FailedCases         int      `json:"failed_cases"`
	SkippedCases        int      `json:"skipped_cases"`
	CoverageRate        float64  `json:"coverage_rate"`
	DefectDetectionRate float64  `json:"defect_detection_rate"`
	CaseQualityScore    float64  `json:"case_quality_score"`
	Summary             string   `json:"summary"`
	Recommendations     []string `json:"recommendations,omitempty"`
}

type Result struct {
	Status   string                 `json:"status"`
	Data     interface{}            `json:"data,omitempty"`
	Error    string                 `json:"error,omitempty"`
	Metadata map[string]interface{} `json:"metadata,omitempty"`
}

type OrchestrationResult struct {
	Task      TestTask      `json:"task"`
	TestCases []TestCase    `json:"test_cases"`
	Results   []TestResult  `json:"results"`
	Quality   QualityReport `json:"quality"`
	Steps     []string      `json:"steps"`
}

type LLMRequest struct {
	Prompt      string
	Temperature float32
	MaxTokens   int
}

type TestCaseExecutor interface {
	Execute(ctx context.Context, testCase TestCase) (ExecutionOutput, error)
}

type ResultCollector interface {
	Collect(result TestResult)
	Results() []TestResult
	Reset()
}

type TaskQueue interface {
	Enqueue(task TestTask) error
	Dequeue(ctx context.Context) (TestTask, error)
	Len() int
}
