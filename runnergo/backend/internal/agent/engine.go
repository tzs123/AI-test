package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

type EngineConfig struct {
	Timeout      time.Duration
	LLMRetries   int
	RetryBackoff time.Duration
	Parallel     int
}

// AgentEngine is the provider-neutral core. Dependencies are interfaces so
// services can use Redis/GORM implementations while tests use in-memory ones.
type AgentEngine struct {
	LLMClient       LLMClient
	KnowledgeBase   *KnowledgeBase
	TaskQueue       TaskQueue
	ResultCollector ResultCollector
	Executor        TestCaseExecutor
	ExecutorConfig  ExecutorConfig
	Executors       map[uint]TestCaseExecutor
	ExecutorConfigs map[uint]ExecutorConfig
	Config          EngineConfig
	OnTaskStart     func(TestTask)
	mu              sync.RWMutex
}

// SetExecutor replaces the active executor for subsequent runs.
func (e *AgentEngine) SetExecutor(executor TestCaseExecutor) {
	if e == nil {
		return
	}
	e.mu.Lock()
	e.Executor = executor
	e.mu.Unlock()
}

func (e *AgentEngine) ExecutorConfigured() bool {
	if e == nil {
		return false
	}
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.Executor != nil
}

func (e *AgentEngine) SubmitTask(task TestTask) error {
	if e == nil || e.TaskQueue == nil {
		return errors.New("task queue is not configured")
	}
	return e.TaskQueue.Enqueue(task)
}

// RunWorker consumes queued tasks until cancellation. Results are delivered
// to onResult, allowing the service layer to persist status and audit events.
func (e *AgentEngine) RunWorker(ctx context.Context, onResult func(OrchestrationResult, error)) {
	if e == nil || e.TaskQueue == nil {
		return
	}
	if ctx == nil {
		ctx = context.Background()
	}
	orchestrator := NewOrchestrator(e)
	for {
		task, err := e.TaskQueue.Dequeue(ctx)
		if err != nil {
			return
		}
		if e.OnTaskStart != nil {
			e.OnTaskStart(task)
		}
		result, runErr := orchestrator.Run(ctx, task)
		if onResult != nil {
			onResult(result, runErr)
		}
		if durable, ok := e.TaskQueue.(DurableTaskQueue); ok {
			if runErr != nil && ctx.Err() == nil {
				_ = durable.Ack(ctx, task)
			} else if ctx.Err() != nil {
				_ = durable.Requeue(context.Background(), task)
			} else {
				_ = durable.Ack(ctx, task)
			}
		}
	}
}

func NewAgentEngine(llm LLMClient, kb *KnowledgeBase, queue TaskQueue, collector ResultCollector) *AgentEngine {
	if kb == nil {
		kb = NewKnowledgeBase()
	}
	if queue == nil {
		queue = NewMemoryTaskQueue(100)
	}
	if collector == nil {
		collector = NewMemoryResultCollector()
	}
	return &AgentEngine{LLMClient: llm, KnowledgeBase: kb, TaskQueue: queue, ResultCollector: collector, Executors: map[uint]TestCaseExecutor{}, ExecutorConfigs: map[uint]ExecutorConfig{},
		ExecutorConfig: ExecutorConfig{Type: "none"},
		Config:         EngineConfig{Timeout: 60 * time.Second, LLMRetries: 2, RetryBackoff: 100 * time.Millisecond, Parallel: 1}}
}

func (e *AgentEngine) GetExecutorConfig() ExecutorConfig {
	if e == nil {
		return ExecutorConfig{Type: "none"}
	}
	e.mu.RLock()
	defer e.mu.RUnlock()
	cfg := e.ExecutorConfig
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	return cfg
}

func (e *AgentEngine) GetExecutorConfigForProject(projectID uint) ExecutorConfig {
	if e == nil {
		return ExecutorConfig{Type: "none", ProjectID: projectID}
	}
	e.mu.RLock()
	defer e.mu.RUnlock()
	if cfg, ok := e.ExecutorConfigs[projectID]; ok {
		return cfg
	}
	cfg := e.ExecutorConfig
	cfg.ProjectID = projectID
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	return cfg
}

// ApplyExecutorConfig validates and activates a persisted executor config.
func (e *AgentEngine) ApplyExecutorConfig(cfg ExecutorConfig) error {
	if e == nil {
		return errors.New("agent engine is nil")
	}
	executor, err := NewExecutorFromConfig(cfg)
	if err != nil {
		return err
	}
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	e.mu.Lock()
	if e.Executors == nil {
		e.Executors = map[uint]TestCaseExecutor{}
	}
	if e.ExecutorConfigs == nil {
		e.ExecutorConfigs = map[uint]ExecutorConfig{}
	}
	e.Executors[cfg.ProjectID] = executor
	e.ExecutorConfigs[cfg.ProjectID] = cfg
	// Keep the legacy default accessor meaningful while project-aware callers
	// resolve from the map above.
	e.Executor, e.ExecutorConfig = executor, cfg
	e.mu.Unlock()
	return nil
}

func (e *AgentEngine) contextWithTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	if parent == nil {
		parent = context.Background()
	}
	t := e.Config.Timeout
	if t <= 0 {
		t = 60 * time.Second
	}
	return context.WithTimeout(parent, t)
}

func (e *AgentEngine) GenerateTestCases(task TestTask) ([]TestCase, error) {
	if e == nil {
		return nil, errors.New("agent engine is nil")
	}
	if strings.TrimSpace(task.Description) == "" {
		return nil, errors.New("task description is required")
	}
	ctx, cancel := e.contextWithTimeout(context.Background())
	defer cancel()
	knowledge := ""
	if e.KnowledgeBase != nil {
		knowledge = e.KnowledgeBase.PromptContext(task.Description)
	}
	prompt := buildTestCasePrompt(task, knowledge)
	client := e.LLMClient
	if client == nil {
		return heuristicCases(task), nil
	}
	wrapped := RetryLLM{Client: client, Retries: e.Config.LLMRetries, Backoff: e.Config.RetryBackoff}
	content, err := wrapped.Generate(ctx, prompt)
	if err != nil {
		// A deterministic fallback still gives users useful cases when a provider
		// is temporarily unavailable, while preserving the provider error context.
		fallback := heuristicCases(task)
		if len(fallback) > 0 {
			return fallback, fmt.Errorf("llm generation failed: %w", err)
		}
		return nil, err
	}
	cases, err := parseTestCases(content)
	if err != nil {
		return heuristicCases(task), fmt.Errorf("parse generated test cases: %w", err)
	}
	cases = normalizeTestCases(cases, task)
	return cases, nil
}

func normalizeTestCases(cases []TestCase, task TestTask) []TestCase {
	if len(cases) == 0 {
		return heuristicCases(task)
	}
	for i := range cases {
		if cases[i].ID == "" {
			cases[i].ID = fmt.Sprintf("TC-%03d", i+1)
		}
		if strings.TrimSpace(cases[i].Title) == "" {
			cases[i].Title = fmt.Sprintf("%s - 用例 %d", task.Name, i+1)
		}
		if len(cases[i].Steps) == 0 {
			cases[i].Steps = []TestStep{{Action: "按需求执行并校验结果"}}
		}
		for j := range cases[i].Steps {
			if strings.TrimSpace(cases[i].Steps[j].Action) == "" {
				cases[i].Steps[j].Action = "按需求执行并校验结果"
			}
		}
		if strings.TrimSpace(cases[i].ExpectedResult) == "" {
			cases[i].ExpectedResult = "系统按需求完成操作并返回符合预期的结果"
		}
		if strings.TrimSpace(cases[i].Description) == "" {
			cases[i].Description = task.Description
		}
	}
	return cases
}

func buildTestCasePrompt(task TestTask, knowledge string) string {
	var b strings.Builder
	b.WriteString("你是测试设计 Agent。根据需求生成覆盖正向、边界、异常场景的 JSON 测试用例。仅返回 JSON 数组，每项字段：id,title,description,scenario_type,priority,preconditions,steps,expected_result,tags。\n需求：")
	b.WriteString(strings.Join(ExtractRequirements(task.Description), "；"))
	if knowledge != "" {
		b.WriteString("\n相关知识：\n")
		b.WriteString(knowledge)
	}
	// /no_think 跳过 qwen3 思维模式（避免推理几百 token 才出 JSON）；
	// 对非 qwen3 模型无害，会被当作普通文本忽略。
	b.WriteString("\n/no_think")
	return b.String()
}

// ExtractRequirements performs a lightweight, deterministic requirement
// extraction before the prompt is sent to an LLM. Newlines and sentence
// boundaries are preserved so acceptance criteria remain visible.
func ExtractRequirements(description string) []string {
	parts := strings.FieldsFunc(description, func(r rune) bool {
		return r == '\n' || r == '。' || r == '.' || r == ';' || r == '；' || r == '！' || r == '!' || r == '？' || r == '?'
	})
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if v := strings.TrimSpace(p); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func parseTestCases(content string) ([]TestCase, error) {
	content = strings.TrimSpace(content)
	if i := strings.Index(content, "["); i >= 0 {
		content = content[i:]
	}
	if i := strings.LastIndex(content, "]"); i >= 0 {
		content = content[:i+1]
	}
	// 清理未转义的控制字符（0x00-0x1F，除 \n \r \t 外），
	// 容忍 qwen3 等模型偶尔输出的非法控制字符导致 json.Unmarshal 失败。
	content = strings.Map(func(r rune) rune {
		if r < 0x20 && r != '\n' && r != '\r' && r != '\t' {
			return -1
		}
		return r
	}, content)
	var cases []TestCase
	if err := json.Unmarshal([]byte(content), &cases); err != nil {
		return nil, err
	}
	for i := range cases {
		if cases[i].ID == "" {
			cases[i].ID = fmt.Sprintf("TC-%03d", i+1)
		}
		if cases[i].ScenarioType == "" {
			cases[i].ScenarioType = "positive"
		}
		if cases[i].Title == "" {
			cases[i].Title = "Generated test case"
		}
	}
	return cases, nil
}

func heuristicCases(task TestTask) []TestCase {
	base := strings.TrimSpace(task.Name)
	if base == "" {
		base = "需求"
	}
	return []TestCase{
		{ID: "TC-001", Title: base + " - 正常流程", Description: task.Description, ScenarioType: "positive", Priority: "P1", Steps: []TestStep{{Action: "按需求执行正常输入"}}, ExpectedResult: "系统成功完成需求描述的业务操作", Tags: []string{"positive"}},
		{ID: "TC-002", Title: base + " - 边界条件", Description: "验证空值、最大最小值及长度边界", ScenarioType: "boundary", Priority: "P1", Steps: []TestStep{{Action: "输入边界值并提交"}}, ExpectedResult: "系统按规范校验边界值并返回明确结果", Tags: []string{"boundary"}},
		{ID: "TC-003", Title: base + " - 异常输入", Description: "验证非法输入、未授权和依赖异常", ScenarioType: "negative", Priority: "P1", Steps: []TestStep{{Action: "输入非法值或模拟依赖失败"}}, ExpectedResult: "系统安全拒绝请求并返回可理解的错误信息", Tags: []string{"negative"}},
	}
}

func (e *AgentEngine) ExecuteTestCases(cases []TestCase) ([]TestResult, error) {
	return e.executeTestCases(cases, 0)
}

// ExecuteTestCasesForProject selects the executor configured for one project.
// It is used by the durable orchestrator path; the legacy method keeps the
// default executor semantics unchanged.
func (e *AgentEngine) ExecuteTestCasesForProject(projectID uint, cases []TestCase) ([]TestResult, error) {
	return e.executeTestCases(cases, projectID)
}

func (e *AgentEngine) executeTestCases(cases []TestCase, projectID uint) ([]TestResult, error) {
	if e == nil {
		return nil, errors.New("agent engine is nil")
	}
	if len(cases) == 0 {
		return []TestResult{}, nil
	}
	ctx, cancel := e.contextWithTimeout(context.Background())
	defer cancel()
	collector := e.ResultCollector
	if collector == nil {
		collector = NewMemoryResultCollector()
		e.ResultCollector = collector
	}
	collector.Reset()
	parallel := e.Config.Parallel
	if parallel < 1 {
		parallel = 1
	}
	if parallel > len(cases) {
		parallel = len(cases)
	}
	jobs := make(chan TestCase)
	var wg sync.WaitGroup
	var firstErr error
	var errMu sync.Mutex
	worker := func() {
		defer wg.Done()
		for tc := range jobs {
			result := e.executeOneForProject(ctx, projectID, tc)
			collector.Collect(result)
			if result.Status == "error" {
				errMu.Lock()
				if firstErr == nil {
					firstErr = errors.New(result.Error)
				}
				errMu.Unlock()
			}
		}
	}
	for i := 0; i < parallel; i++ {
		wg.Add(1)
		go worker()
	}
enqueue:
	for _, tc := range cases {
		select {
		case jobs <- tc:
		case <-ctx.Done():
			break enqueue
		}
	}
	close(jobs)
	wg.Wait()
	results := collector.Results()
	if firstErr != nil {
		return results, firstErr
	}
	if err := ctx.Err(); err != nil {
		return results, err
	}
	return results, nil
}

func (e *AgentEngine) executeOne(ctx context.Context, tc TestCase) TestResult {
	return e.executeOneForProject(ctx, 0, tc)
}

func (e *AgentEngine) executeOneForProject(ctx context.Context, projectID uint, tc TestCase) TestResult {
	started := time.Now()
	r := TestResult{CaseID: tc.ID, Title: tc.Title, Expected: tc.ExpectedResult, StartedAt: started}
	var out ExecutionOutput
	var err error
	e.mu.RLock()
	executor := e.Executor
	if projectID != 0 && e.Executors != nil {
		if configured, ok := e.Executors[projectID]; ok {
			executor = configured
		}
	}
	e.mu.RUnlock()
	if executor != nil {
		out, err = executor.Execute(ctx, tc)
	} else {
		r.FinishedAt = time.Now()
		r.Duration = r.FinishedAt.Sub(started)
		r.Status = "skipped"
		r.Logs = "test executor is not configured"
		return r
	}
	r.FinishedAt = time.Now()
	r.Duration = r.FinishedAt.Sub(started)
	r.Actual = out.Actual
	r.Logs = out.Logs
	if err != nil {
		r.Status = "error"
		r.Error = err.Error()
		return r
	}
	if out.Passed != nil {
		r.Status = "failed"
		if *out.Passed {
			r.Status = "passed"
		}
	} else if strings.TrimSpace(out.Actual) == strings.TrimSpace(tc.ExpectedResult) {
		r.Status = "passed"
	} else {
		r.Status = "failed"
	}
	return r
}
