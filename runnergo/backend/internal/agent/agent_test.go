package agent

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

type mockLLM struct {
	response string
	err      error
	calls    int
	mu       sync.Mutex
}
type completeLLM struct{}

func (completeLLM) Complete(string) (string, error) { return "complete", nil }

type chatLLM struct{}

func (chatLLM) Chat(context.Context, string) (string, error) { return "chat", nil }

func (m *mockLLM) Generate(ctx context.Context, prompt string) (string, error) {
	m.mu.Lock()
	m.calls++
	m.mu.Unlock()
	if m.err != nil {
		return "", m.err
	}
	return m.response, nil
}

type mapExecutor struct {
	actual map[string]string
	err    error
}

func (m mapExecutor) Execute(_ context.Context, tc TestCase) (ExecutionOutput, error) {
	if m.err != nil {
		return ExecutionOutput{}, m.err
	}
	return ExecutionOutput{Actual: m.actual[tc.ID], Logs: "log"}, nil
}

type defectLoader struct{}

func (defectLoader) LoadDefects() ([]Knowledge, error) {
	return []Knowledge{{Title: "Login defect", Type: "bug", Content: "token expiry"}}, nil
}

type fakeMigrator struct{ n int }

func (m *fakeMigrator) AutoMigrate(dst ...interface{}) error { m.n = len(dst); return nil }

type fakeRedis struct{ values []string }

func (r *fakeRedis) LPush(_ context.Context, _ string, values ...interface{}) (int64, error) {
	for _, v := range values {
		r.values = append([]string{v.(string)}, r.values...)
	}
	return int64(len(r.values)), nil
}
func (r *fakeRedis) BRPop(_ context.Context, _ time.Duration, _ ...string) ([]string, error) {
	if len(r.values) == 0 {
		return nil, errors.New("empty")
	}
	v := r.values[len(r.values)-1]
	r.values = r.values[:len(r.values)-1]
	return []string{"key", v}, nil
}
func (r *fakeRedis) LLen(_ context.Context, _ string) (int64, error) {
	return int64(len(r.values)), nil
}

func TestGenerateCasesWithLLMAndFallback(t *testing.T) {
	llm := &mockLLM{response: `[{"title":"ok","expected_result":"done"}]`}
	kb := NewKnowledgeBase()
	kb.Add(Knowledge{Title: "login", Content: "password policy"})
	e := NewAgentEngine(llm, kb, nil, nil)
	e.Config.Timeout = time.Second
	cases, err := e.GenerateTestCases(TestTask{Name: "Login", Description: "login password"})
	if err != nil || len(cases) != 1 || cases[0].ID != "TC-001" {
		t.Fatalf("cases=%+v err=%v", cases, err)
	}
	if llm.calls != 1 {
		t.Fatal(llm.calls)
	}
	e.LLMClient = &mockLLM{err: errors.New("down")}
	cases, err = e.GenerateTestCases(TestTask{Description: "something"})
	if err == nil || len(cases) != 3 {
		t.Fatalf("fallback cases=%d err=%v", len(cases), err)
	}
	if _, err = e.GenerateTestCases(TestTask{}); err == nil {
		t.Fatal("empty description should fail")
	}
	if out, err := callLLM(context.Background(), completeLLM{}, "x"); err != nil || out != "complete" {
		t.Fatal(out, err)
	}
	if out, err := callLLM(context.Background(), chatLLM{}, "x"); err != nil || out != "chat" {
		t.Fatal(out, err)
	}
	if _, err := callLLM(context.Background(), struct{}{}, "x"); err == nil {
		t.Fatal("unsupported client")
	}
	parsed, err := parseTestCases(`[{"preconditions":{"value":"logged out"},"steps":"submit","expected_result":"ok"}]`)
	if err != nil || len(parsed) != 1 || len(parsed[0].Preconditions) != 1 || len(parsed[0].Steps) != 1 {
		t.Fatal(parsed, err)
	}
	if got := ExtractRequirements("登录。输入错误;重试？"); len(got) != 3 {
		t.Fatal(got)
	}
}

func TestExecuteCasesAndQuality(t *testing.T) {
	e := NewAgentEngine(nil, nil, nil, NewMemoryResultCollector())
	e.Executor = mapExecutor{actual: map[string]string{"1": "yes", "2": "no"}}
	e.Config.Parallel = 2
	results, err := e.ExecuteTestCases([]TestCase{{ID: "1", Title: "pass", ExpectedResult: "yes"}, {ID: "2", Title: "fail", ExpectedResult: "yes"}})
	if err != nil || len(results) != 2 {
		t.Fatalf("%+v %v", results, err)
	}
	q := e.EvaluateQuality(results)
	if q.PassedCases != 1 || q.FailedCases != 1 || q.CoverageRate != 1 || q.DefectDetectionRate != .5 {
		t.Fatalf("%+v", q)
	}
	e.Executor = mapExecutor{err: errors.New("boom")}
	results, err = e.ExecuteTestCases([]TestCase{{ID: "3", ExpectedResult: "x"}})
	if err == nil || results[0].Status != "error" {
		t.Fatalf("%+v %v", results, err)
	}
	if got := e.EvaluateQuality(nil); got.CoverageRate != 0 || got.TotalCases != 0 {
		t.Fatal(got)
	}
	e.LLMClient = &mockLLM{response: "quality 85"}
	if q := e.EvaluateQuality(results); q.CaseQualityScore != 85 {
		t.Fatal(q)
	}
	e.Executor = nil
	skipped, err := e.ExecuteTestCases([]TestCase{{ID: "skip", ExpectedResult: "x"}})
	if err != nil || len(skipped) != 1 || skipped[0].Status != "skipped" {
		t.Fatalf("skipped=%+v err=%v", skipped, err)
	}
	if q := e.EvaluateQuality(skipped); q.ExecutedCases != 0 || q.SkippedCases != 1 || q.CoverageRate != 0 || q.CaseQualityScore != 0 {
		t.Fatalf("skipped quality=%+v", q)
	}
	if report := RenderQualityReport(e.EvaluateQuality(results)); !strings.Contains(report, "智能测试质量报告") {
		t.Fatal(report)
	}
}

func TestKnowledgeBaseLoadRetrieve(t *testing.T) {
	d := t.TempDir()
	md := filepath.Join(d, "guide.md")
	if err := os.WriteFile(md, []byte("# Login\nUse MFA and password."), 0600); err != nil {
		t.Fatal(err)
	}
	kb := NewKnowledgeBase()
	if err := kb.LoadDocument(md); err != nil {
		t.Fatal(err)
	}
	yaml := filepath.Join(d, "rules.yaml")
	if err := os.WriteFile(yaml, []byte("- name: auth\n  rule: require MFA\n- title: api\n  content: check status\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := kb.LoadStandardsYAML(yaml); err != nil {
		t.Fatal(err)
	}
	if err := kb.LoadHistoricalDefects(defectLoader{}); err != nil {
		t.Fatal(err)
	}
	if len(kb.Retrieve("MFA login")) == 0 || kb.PromptContext("token expiry") == "" {
		t.Fatal("retrieval failed")
	}
	if err := kb.LoadDocuments(d); err != nil {
		t.Fatal(err)
	}
	pdf := filepath.Join(d, "x.pdf")
	if err := os.WriteFile(pdf, []byte("BT (PDF login rule) Tj ET"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := kb.LoadDocument(pdf); err != nil {
		t.Fatal(err)
	}
	if len(tokenize("alpha beta-gamma")) != 3 {
		t.Fatal(tokenize("alpha beta-gamma"))
	}
	if len(parseSimpleYAML("")) != 0 {
		t.Fatal("empty yaml")
	}
}

func TestQueueAndMigration(t *testing.T) {
	q := NewMemoryTaskQueue(1)
	if q.Len() != 0 {
		t.Fatal()
	}
	task := TestTask{ID: "a"}
	if err := q.Enqueue(task); err != nil {
		t.Fatal(err)
	}
	if err := q.Enqueue(task); err == nil {
		t.Fatal("expected full")
	}
	got, err := q.Dequeue(context.Background())
	if err != nil || got.ID != "a" {
		t.Fatal(got, err)
	}
	q.Close()
	if err := q.Enqueue(task); err == nil {
		t.Fatal("expected closed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := q.Dequeue(ctx); err == nil {
		t.Fatal("expected cancellation")
	}
	m := &fakeMigrator{}
	if err := Migrate(m); err != nil || m.n != 6 {
		t.Fatal(m.n, err)
	}
	if err := Migrate(nil); err != nil {
		t.Fatal(err)
	}
	if err := MigrateExecutorConfig(m); err != nil || m.n != 1 {
		t.Fatalf("executor migration=%d err=%v", m.n, err)
	}
	redis := &fakeRedis{}
	rq := RedisTaskQueue{Client: redis, Key: "q"}
	if err := rq.Enqueue(task); err != nil {
		t.Fatal(err)
	}
	if rq.Len() != 1 {
		t.Fatal(rq.Len())
	}
	got, err = rq.Dequeue(context.Background())
	if err != nil || got.ID != "a" {
		t.Fatal(got, err)
	}
	if rq.Len() != 0 {
		t.Fatal()
	}
	if err := (RedisTaskQueue{}).Enqueue(task); err == nil {
		t.Fatal("nil redis")
	}
	if _, err := (RedisTaskQueue{}).Dequeue(context.Background()); err == nil {
		t.Fatal("nil redis")
	}
	if _, err := (MySQLDefectLoader{}).LoadDefects(); err == nil {
		t.Fatal("nil mysql")
	}
	if (&AgentTaskModel{}).TableName() != "agent_tasks" || (&AgentTestCaseModel{}).TableName() != "agent_test_cases" || (&AgentTestResultModel{}).TableName() != "agent_test_results" || (&KnowledgeDocumentModel{}).TableName() != "knowledge_documents" || (&KnowledgeChunkModel{}).TableName() != "knowledge_chunks" || (&AgentMemoryModel{}).TableName() != "agent_memories" || (&AgentExecutorConfigModel{}).TableName() != "agent_executor_configs" {
		t.Fatal("table names")
	}
}

func TestConfigAndWorker(t *testing.T) {
	t.Setenv("RUNNERGO_AGENT_TIMEOUT", "2s")
	t.Setenv("RUNNERGO_AGENT_RETRIES", "3")
	t.Setenv("RUNNERGO_AGENT_RETRY_BACKOFF", "2ms")
	t.Setenv("RUNNERGO_AGENT_PARALLEL", "2")
	t.Setenv("RUNNERGO_LLM_PROVIDER", "")
	c := ConfigFromEnv()
	if c.Timeout != 2*time.Second || c.LLMRetries != 3 || c.Parallel != 2 || ProviderFromEnv() != "" {
		t.Fatal(c, ProviderFromEnv())
	}
	e := NewEngineFromEnv(nil, nil, nil)
	if e.LLMClient != nil {
		t.Fatal("empty provider should use fallback")
	}
	t.Setenv("RUNNERGO_LLM_PROVIDER", "openai")
	t.Setenv("RUNNERGO_LLM_API_KEY", "key")
	t.Setenv("RUNNERGO_LLM_MODEL", "model")
	t.Setenv("RUNNERGO_LLM_BASE_URL", "http://llm")
	t.Setenv("RUNNERGO_EXECUTOR_TYPE", "command")
	t.Setenv("RUNNERGO_EXECUTOR_ENABLED", "true")
	t.Setenv("RUNNERGO_EXECUTOR_COMMAND", "printf ok")
	t.Setenv("RUNNERGO_EXECUTOR_ALLOWED_COMMANDS", "printf,echo")
	t.Setenv("RUNNERGO_EXECUTOR_HEADERS", `{"X-Test":"yes"}`)
	t.Setenv("RUNNERGO_EXECUTOR_EXPECTED_STATUS", "201")
	t.Setenv("RUNNERGO_EXECUTOR_TIMEOUT_MS", "500")
	envCfg := ExecutorConfigFromEnv()
	if envCfg.Type != "command" || !envCfg.Enabled || len(envCfg.AllowedCommands) != 2 || envCfg.Headers["X-Test"] != "yes" {
		t.Fatalf("env config=%+v", envCfg)
	}
	open := NewEngineFromEnv(nil, nil, nil)
	if _, ok := open.LLMClient.(*OpenAIClient); !ok || !open.ExecutorConfigured() {
		t.Fatalf("env engine=%T configured=%v", open.LLMClient, open.ExecutorConfigured())
	}
	t.Setenv("RUNNERGO_LLM_PROVIDER", "ollama")
	local := NewEngineFromEnv(nil, nil, nil)
	if _, ok := local.LLMClient.(*LocalModelClient); !ok {
		t.Fatalf("local llm=%T", local.LLMClient)
	}
	q := NewMemoryTaskQueue(1)
	e.TaskQueue = q
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go e.RunWorker(ctx, func(result OrchestrationResult, err error) { close(done) })
	if err := e.SubmitTask(TestTask{Description: "worker"}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("worker did not run")
	}
	cancel()
}

func TestOrchestratorAndRetry(t *testing.T) {
	llm := &mockLLM{response: `[{"id":"x","title":"x","expected_result":"ok"}]`}
	e := NewAgentEngine(llm, nil, nil, nil)
	o := NewOrchestrator(e)
	r, err := o.Run(context.Background(), TestTask{Description: "x"})
	if err != nil || len(r.Results) != 1 || r.Quality.TotalCases != 1 {
		t.Fatalf("%+v %v", r, err)
	}
	if got := o.Process(TestTask{Description: "x"}); got.Status != "success" {
		t.Fatal(got)
	}
	o.RegisterAgent(nil)
	if _, err := o.Run(context.Background(), TestTask{}); err == nil {
		t.Fatal("expected design error")
	}
	e.Config.LLMRetries = 1
	e.Config.RetryBackoff = time.Millisecond
	e.LLMClient = &mockLLM{err: errors.New("x")}
	if _, err := (RetryLLM{Client: e.LLMClient, Retries: 1, Backoff: time.Millisecond}).Generate(context.Background(), "x"); err == nil {
		t.Fatal()
	}
}

func TestHTTPClients(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(r.URL.Path, "chat/completions") {
			w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}]}`))
		} else {
			w.Write([]byte(`{"response":"local"}`))
		}
	}))
	defer ts.Close()
	if got, err := (&OpenAIClient{APIKey: "k", BaseURL: ts.URL}).Generate(context.Background(), "x"); err != nil || got != "ok" {
		t.Fatal(got, err)
	}
	if got, err := (&LocalModelClient{BaseURL: ts.URL}).Generate(context.Background(), "x"); err != nil || got != "local" {
		t.Fatal(got, err)
	}
}

func TestAgentsAndScriptExecution(t *testing.T) {
	e := NewAgentEngine(nil, nil, nil, nil)
	d, x, v := &DesignAgent{Engine: e}, &ExecuteAgent{Engine: e}, &EvaluateAgent{Engine: e}
	if d.Name() == "" || d.Role() == "" || x.Name() == "" || x.Role() == "" || v.Name() == "" || v.Role() == "" {
		t.Fatal("agent metadata")
	}
	if got := d.Process(TestTask{Description: "x"}); got.Status != "success" {
		t.Fatal(got)
	}
	if got := x.Process(TestTask{}); got.Status != "failed" {
		t.Fatal(got)
	}
	if got := v.Process(TestTask{}); got.Status != "success" {
		t.Fatal(got)
	}
	if got := (&DesignAgent{}).Process(TestTask{}); got.Status != "failed" {
		t.Fatal(got)
	}
	if got := (&ExecuteAgent{}).Process(TestTask{}); got.Status != "failed" {
		t.Fatal(got)
	}
	if got := (&EvaluateAgent{}).Process(TestTask{}); got.Status != "failed" {
		t.Fatal(got)
	}
	e.Executor = CommandExecutor{AllowedCommands: map[string]bool{"printf": true}}
	results, err := e.ExecuteTestCases([]TestCase{{ID: "s", ExpectedResult: "ok", Script: "printf ok"}})
	if err != nil || results[0].Status != "passed" {
		t.Fatal(results, err)
	}
	e.Executor = CommandExecutor{AllowedCommands: map[string]bool{}}
	if _, err := e.ExecuteTestCases([]TestCase{{ID: "d", Script: "rm anything"}}); err == nil {
		t.Fatal("command must be denied")
	}
}

func TestAPIExecutorAndConfig(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret" {
			http.Error(w, "missing token", http.StatusUnauthorized)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte("created"))
	}))
	defer ts.Close()
	cfg := ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, Method: "POST", ExpectedStatus: http.StatusCreated, TimeoutMS: 1000, Token: "secret"}
	e, err := NewExecutorFromConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	out, err := e.Execute(context.Background(), TestCase{ID: "api", Script: "/users"})
	if err != nil || out.Passed == nil || !*out.Passed || out.Data["status_code"] != http.StatusCreated {
		t.Fatalf("out=%+v err=%v", out, err)
	}
	if _, err := NewExecutorFromConfig(ExecutorConfig{Enabled: true, Type: "api", BaseURL: "file:///tmp"}); err == nil {
		t.Fatal("unsafe URL accepted")
	}
	if _, err := NewExecutorFromConfig(ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, ExpectedStatus: 600}); err == nil {
		t.Fatal("invalid status accepted")
	}
	if _, err := NewExecutorFromConfig(ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, ExpectedStatus: 99}); err == nil {
		t.Fatal("low status accepted")
	}
	if _, err := NewExecutorFromConfig(ExecutorConfig{Enabled: true, Type: "command"}); err == nil {
		t.Fatal("missing command allow-list accepted")
	}
	if ex, err := NewExecutorFromConfig(ExecutorConfig{Enabled: false, Type: "none"}); err != nil || ex != nil {
		t.Fatalf("none=%v %v", ex, err)
	}
	if _, err := NewExecutorFromConfig(ExecutorConfig{Enabled: true, Type: "wat"}); err == nil {
		t.Fatal("unknown type accepted")
	}
	if _, err := (APIExecutor{Config: cfg}).Execute(context.Background(), TestCase{Script: "http://example.com/escape"}); err == nil {
		t.Fatal("cross-origin URL accepted")
	}
	if _, err := (APIExecutor{Config: ExecutorConfig{BaseURL: ts.URL, Method: "TRACE"}}).Execute(context.Background(), TestCase{}); err == nil {
		t.Fatal("unsafe method execution accepted")
	}
	if _, err := (CommandExecutor{}).Execute(context.Background(), TestCase{}); err == nil {
		t.Fatal("empty command accepted")
	}
	if _, err := (CommandExecutor{AllowedCommands: map[string]bool{"printf": true}}).Execute(context.Background(), TestCase{Script: "echo hi"}); err == nil {
		t.Fatal("command allow-list bypass")
	}
	if _, err := (ConfiguredCommandExecutor{DefaultCommand: "printf ok", CommandExecutor: CommandExecutor{AllowedCommands: map[string]bool{"printf": true}}}).Execute(context.Background(), TestCase{}); err != nil {
		t.Fatal(err)
	}
	if err := ValidateExecutorConfig(ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, TimeoutMS: -1}); err == nil {
		t.Fatal("negative timeout accepted")
	}
	if err := ValidateExecutorConfig(ExecutorConfig{Enabled: true, Type: "none"}); err != nil {
		t.Fatal(err)
	}
	if err := ValidateExecutorConfig(ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, Method: "TRACE"}); err == nil {
		t.Fatal("unsafe method accepted")
	}
	store := NewMemoryExecutorConfigStore()
	if got, err := store.Save(context.Background(), cfg); err != nil || got.BaseURL != cfg.BaseURL {
		t.Fatal(got, err)
	}
	if got, err := store.Load(context.Background(), 0); err != nil || got.Type != "api" {
		t.Fatal(got, err)
	}
	if got, _ := store.Load(context.Background(), 0); got.Token != "secret" {
		t.Fatalf("token persistence=%q", got.Token)
	}
	engine := NewAgentEngine(nil, nil, nil, nil)
	if err := engine.ApplyExecutorConfig(cfg); err != nil || !engine.ExecutorConfigured() || engine.GetExecutorConfig().Type != "api" {
		t.Fatal(err, engine.GetExecutorConfig())
	}
	engine.SetExecutor(nil)
	if engine.ExecutorConfigured() {
		t.Fatal("executor should be disabled")
	}
	if err := engine.ApplyExecutorConfig(ExecutorConfig{Enabled: true, Type: "wat"}); err == nil {
		t.Fatal("invalid engine config accepted")
	}
	row, err := executorConfigToModel(cfg)
	if err != nil || row.Token != "secret" {
		t.Fatal(row, err)
	}
	decoded, err := executorConfigFromModel(row)
	if err != nil || decoded.Token != "secret" {
		t.Fatal(decoded, err)
	}
	if _, err := (GORMExecutorConfigStore{}).Load(context.Background(), 0); err == nil {
		t.Fatal("nil gorm load")
	}
	if _, err := (GORMExecutorConfigStore{}).Save(context.Background(), cfg); err == nil {
		t.Fatal("nil gorm save")
	}
	db, err := gorm.Open(sqlite.Open("file::memory:?cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&AgentExecutorConfigModel{}); err != nil {
		t.Fatal(err)
	}
	gormStore := GORMExecutorConfigStore{DB: db}
	cfg.ProjectID = 77
	saved, err := gormStore.Save(context.Background(), cfg)
	if err != nil || saved.ID == 0 {
		t.Fatal(saved, err)
	}
	cfg.Method = "PATCH"
	if _, err = gormStore.Save(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	loaded, err := gormStore.Load(context.Background(), 77)
	if err != nil || loaded.Method != "PATCH" || loaded.Token != "secret" {
		t.Fatal(loaded, err)
	}
	if all, err := gormStore.LoadAll(context.Background()); err != nil || len(all) != 1 {
		t.Fatalf("all=%+v err=%v", all, err)
	}
	missing, err := gormStore.Load(context.Background(), 78)
	if err != nil || missing.Type != "none" {
		t.Fatal(missing, err)
	}
}

func TestDurableAgentTaskStore(t *testing.T) {
	db, err := gorm.Open(sqlite.Open("file:agent-store-test?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	store := GORMAgentTaskStore{DB: db}
	if err := Migrate(db); err != nil {
		t.Fatal(err)
	}
	task := TestTask{ID: "durable-1", Name: "登录", Description: "验证登录", ProjectID: 12, Context: map[string]interface{}{"created_by": 7, "target": "api"}}
	if err := store.CreateTask(context.Background(), task, "queued"); err != nil {
		t.Fatal(err)
	}
	if err := store.SetStatus(context.Background(), task.ID, "running"); err != nil {
		t.Fatal(err)
	}
	if _, status, err := store.LoadResult(context.Background(), task.ID, task.ProjectID); err != nil || status != "running" {
		t.Fatalf("status=%s err=%v", status, err)
	}
	cases := []TestCase{{ID: "TC-1", Title: "正常登录", Description: "ok", ScenarioType: "positive", Priority: "P1", Steps: []TestStep{{Action: "POST", Data: "{}"}}, ExpectedResult: "ok", Script: "/login", Tags: []string{"smoke"}, Metadata: map[string]interface{}{"method": "POST"}}}
	passed := true
	result := OrchestrationResult{Task: task, TestCases: cases, Results: []TestResult{{CaseID: "TC-1", Status: "passed", Expected: "ok", Actual: "ok", StartedAt: time.Now(), FinishedAt: time.Now()}}, Quality: QualityReport{TotalCases: 1, ExecutedCases: 1, PassedCases: 1}, Steps: []string{"design-agent", "execute-agent", "evaluate-agent"}}
	if err := store.SaveResult(context.Background(), task.ID, result, "passed", nil); err != nil {
		t.Fatal(err)
	}
	loaded, status, err := store.LoadResult(context.Background(), task.ID, task.ProjectID)
	if err != nil || status != "passed" || len(loaded.TestCases) != 1 || loaded.TestCases[0].Script != "/login" || !passed {
		t.Fatalf("loaded=%+v status=%s err=%v", loaded, status, err)
	}
	list, err := store.ListResults(context.Background(), task.ProjectID, 10)
	if err != nil || len(list) != 1 {
		t.Fatalf("list=%+v err=%v", list, err)
	}
	k := Knowledge{Title: "登录规范", Type: "standard", Content: "密码必须加密", Metadata: map[string]interface{}{"project_id": float64(12)}}
	if err := store.SaveKnowledge(context.Background(), k); err != nil {
		t.Fatal(err)
	}
	knowledge, err := store.LoadKnowledge(context.Background(), 12)
	if err != nil || len(knowledge) != 1 || knowledge[0].Content != k.Content {
		t.Fatalf("knowledge=%+v err=%v", knowledge, err)
	}
	if _, _, err := store.LoadResult(context.Background(), task.ID, 99); err == nil {
		t.Fatal("cross-project task should not load")
	}
	if cfg := NewAgentEngine(nil, nil, nil, nil).GetExecutorConfigForProject(55); cfg.ProjectID != 55 || cfg.Type != "none" {
		t.Fatalf("default project config=%+v", cfg)
	}
	configured := NewAgentEngine(nil, nil, nil, nil)
	if err := configured.ApplyExecutorConfig(ExecutorConfig{ProjectID: 55, Enabled: false, Type: "none"}); err != nil {
		t.Fatal(err)
	}
	if cfg := configured.GetExecutorConfigForProject(55); cfg.ProjectID != 55 || cfg.Type != "none" {
		t.Fatalf("configured project config=%+v", cfg)
	}
}

func TestProjectExecutorSelection(t *testing.T) {
	e := NewAgentEngine(nil, nil, nil, nil)
	e.Config.Parallel = 1
	first := mapExecutor{actual: map[string]string{"x": "one"}}
	second := mapExecutor{actual: map[string]string{"x": "two"}}
	e.ApplyExecutorConfig(ExecutorConfig{Enabled: true, Type: "command", ProjectID: 1, AllowedCommands: []string{"printf"}, Command: "printf one"})
	e.SetExecutor(first)
	e.ApplyExecutorConfig(ExecutorConfig{Enabled: true, Type: "command", ProjectID: 2, AllowedCommands: []string{"printf"}, Command: "printf two"})
	// Explicitly install test doubles into the project map to verify routing.
	e.mu.Lock()
	e.Executors[1] = first
	e.Executors[2] = second
	e.mu.Unlock()
	result, err := e.ExecuteTestCasesForProject(2, []TestCase{{ID: "x", ExpectedResult: "two"}})
	if err != nil || len(result) != 1 || result[0].Actual != "two" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestEngineHelpersAndWorker(t *testing.T) {
	e := NewAgentEngine(nil, nil, nil, nil)
	if got := e.executeOne(context.Background(), TestCase{ID: "none", ExpectedResult: "x"}); got.Status != "skipped" {
		t.Fatalf("got=%+v", got)
	}
	for _, item := range []struct {
		s    string
		want int
	}{{"queued", 5}, {"running", 25}, {"passed", 100}, {"failed", 100}, {"other", 0}} {
		if progressForStatus(item.s) != item.want {
			t.Fatalf("progress %s", item.s)
		}
	}
	if got := normalizeTestCases(nil, TestTask{Name: "n", Description: "d"}); len(got) != 3 {
		t.Fatalf("normalized=%d", len(got))
	}
	q := NewMemoryTaskQueue(2)
	e.TaskQueue = q
	e.OnTaskStart = nil
	started := make(chan struct{})
	_ = e.SubmitTask(TestTask{ID: "worker", Description: "d"})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.RunWorker(ctx, func(OrchestrationResult, error) { close(started); cancel() })
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("worker did not process")
	}
}

func TestDurableRedisQueueLifecycle(t *testing.T) {
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatal(err)
	}
	defer mr.Close()
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	q := NewDurableRedisTaskQueue(client, "test:agent")
	task := TestTask{ID: "redis-1", Description: "queued"}
	if err := q.Enqueue(task); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 {
		t.Fatalf("len=%d", q.Len())
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	got, err := q.Dequeue(ctx)
	if err != nil || got.ID != task.ID {
		t.Fatalf("got=%+v err=%v", got, err)
	}
	if err := q.Ack(ctx, task); err != nil {
		t.Fatal(err)
	}
	if err := q.Enqueue(task); err != nil {
		t.Fatal(err)
	}
	if _, err := q.Dequeue(ctx); err != nil {
		t.Fatal(err)
	}
	if err := q.Requeue(ctx, task); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 {
		t.Fatalf("requeue len=%d", q.Len())
	}
	if _, err := q.Dequeue(ctx); err != nil {
		t.Fatal(err)
	}
	if err := q.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	if q.Len() != 1 {
		t.Fatalf("recover len=%d", q.Len())
	}
	if err := q.Ack(ctx, task); err != nil {
		t.Fatal(err)
	}
	if err := (&DurableRedisTaskQueue{}).Enqueue(task); err == nil {
		t.Fatal("nil client enqueue should fail")
	}
	if err := (&DurableRedisTaskQueue{}).Recover(ctx); err == nil {
		t.Fatal("nil client recover should fail")
	}
}
