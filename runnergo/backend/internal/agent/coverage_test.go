package agent

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	_ "github.com/mattn/go-sqlite3"
	"github.com/redis/go-redis/v9"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

// genOnlyLLM exposes the context-free Generate(string) signature so callLLM
// exercises the no-context adapter path.
type genOnlyLLM struct {
	resp string
	err  error
}

func (g genOnlyLLM) Generate(prompt string) (string, error) { return g.resp, g.err }

// blockingGenLLM blocks long enough for a pre-cancelled context to win the
// select inside callWithoutContext.
type blockingGenLLM struct{}

func (blockingGenLLM) Generate(string) (string, error) {
	time.Sleep(500 * time.Millisecond)
	return "late", nil
}

// shortRedis returns a single-element slice to trigger the len(v) < 2 branch.
type shortRedis struct{}

func (shortRedis) LPush(context.Context, string, ...interface{}) (int64, error) {
	return 1, nil
}
func (shortRedis) BRPop(context.Context, time.Duration, ...string) ([]string, error) {
	return []string{"only"}, nil
}

// noopAgent is a minimal Agent used to exercise RegisterAgent with a real value.
type noopAgent struct{}

func (noopAgent) Name() string             { return "noop-agent" }
func (noopAgent) Role() string             { return "辅助Agent" }
func (noopAgent) Process(task Task) Result { return Result{Status: "success", Data: task} }

func TestMySQLDefectLoaderLoadDefects(t *testing.T) {
	db, err := sql.Open("sqlite3", "file:defects_cov?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("CREATE TABLE IF NOT EXISTS defects (id TEXT, title TEXT, description TEXT, severity TEXT, status TEXT)"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("DELETE FROM defects"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO defects (id,title,description,severity,status) VALUES ('1','登录缺陷','token 过期','high','open')"); err != nil {
		t.Fatal(err)
	}
	items, err := MySQLDefectLoader{DB: db}.LoadDefects()
	if err != nil || len(items) != 1 || items[0].Title != "登录缺陷" || items[0].ID != "defect-1" {
		t.Fatalf("items=%+v err=%v", items, err)
	}
	items, err = MySQLDefectLoader{DB: db, Query: "SELECT id,title,description,severity,status FROM defects WHERE id='1'"}.LoadDefects()
	if err != nil || len(items) != 1 {
		t.Fatalf("custom query items=%+v err=%v", items, err)
	}
	if _, err := (MySQLDefectLoader{DB: db, Query: "SELECT nope FROM defects"}).LoadDefects(); err == nil {
		t.Fatal("expected query error")
	}
}

func TestRedisTaskQueueDequeueEdges(t *testing.T) {
	// default key path
	rq := RedisTaskQueue{Client: &fakeRedis{}}
	if rq.key() != "runnergo:agent:tasks" {
		t.Fatalf("default key=%q", rq.key())
	}
	// single-element response triggers len(v) < 2
	shortRq := RedisTaskQueue{Client: shortRedis{}, Key: "q"}
	if _, err := shortRq.Dequeue(context.Background()); err == nil {
		t.Fatal("expected malformed redis task error")
	}
	// undecodable payload triggers json.Unmarshal error
	bad := &fakeRedis{values: []string{"{not-json}"}}
	badRq := RedisTaskQueue{Client: bad, Key: "q"}
	if _, err := badRq.Dequeue(context.Background()); err == nil {
		t.Fatal("expected decode error")
	}
}

func TestDurableRedisTaskQueueDequeueEdges(t *testing.T) {
	if _, err := (&DurableRedisTaskQueue{}).Dequeue(context.Background()); err == nil {
		t.Fatal("nil client dequeue should fail")
	}
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatal(err)
	}
	defer mr.Close()
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	q := NewDurableRedisTaskQueue(client, "cov:durable")
	// pre-cancelled context exercises the non-redis.Nil return path
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := q.Dequeue(ctx); err == nil {
		t.Fatal("expected context cancelled error")
	}
	// invalid payload exercises the decode-error + LRem cleanup branch
	if err := client.LPush(context.Background(), q.QueueKey, "not-json").Err(); err != nil {
		t.Fatal(err)
	}
	if _, err := q.Dequeue(context.Background()); err == nil {
		t.Fatal("expected decode error")
	}
	// processing list should be empty after the cleanup
	if n, _ := client.LLen(context.Background(), q.ProcessingKey).Result(); n != 0 {
		t.Fatalf("processing not cleaned, len=%d", n)
	}
}

func TestRegisterAgentAppends(t *testing.T) {
	e := NewAgentEngine(nil, nil, nil, nil)
	o := NewOrchestrator(e)
	before := len(o.Agents)
	o.RegisterAgent(noopAgent{})
	if len(o.Agents) != before+1 {
		t.Fatalf("agents=%d before=%d", len(o.Agents), before)
	}
	o.RegisterAgent(nil) // nil must be ignored
	if len(o.Agents) != before+1 {
		t.Fatalf("nil agent should not append, agents=%d", len(o.Agents))
	}
}

func TestAPIExecutorMetadataOverrides(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	defer ts.Close()
	cfg := ExecutorConfig{Enabled: true, Type: "api", BaseURL: ts.URL, Method: "GET"}
	ex := APIExecutor{Config: cfg}
	// plain success + empty target falls back to BaseURL
	if out, err := ex.Execute(context.Background(), TestCase{ID: "empty", ExpectedResult: "ok"}); err != nil || out.Passed == nil || !*out.Passed {
		t.Fatalf("empty target out=%+v err=%v", out, err)
	}
	cases := []TestCase{
		{ID: "script", Script: "/users", ExpectedResult: "ok"},
		{ID: "method", Metadata: map[string]interface{}{"method": "POST"}},
		{ID: "url", Metadata: map[string]interface{}{"url": ts.URL + "/custom", "expected_status": 200}},
		{ID: "path", Metadata: map[string]interface{}{"path": "/p", "expected_status": float64(200)}},
		{ID: "body-str", Metadata: map[string]interface{}{"body": "payload", "expected_status": "200"}},
		{ID: "body-obj", Metadata: map[string]interface{}{"body": map[string]interface{}{"k": "v"}}},
	}
	for _, tc := range cases {
		out, err := ex.Execute(context.Background(), tc)
		if err != nil || out.Passed == nil || !*out.Passed {
			t.Fatalf("case=%s out=%+v err=%v", tc.ID, out, err)
		}
	}
}

func TestCallLLMNoContextVariants(t *testing.T) {
	if out, err := callLLM(context.Background(), genOnlyLLM{resp: "gen"}, "x"); err != nil || out != "gen" {
		t.Fatalf("genOnly out=%q err=%v", out, err)
	}
	if _, err := callLLM(context.Background(), genOnlyLLM{err: errors.New("boom")}, "x"); err == nil {
		t.Fatal("expected error propagation")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := callLLM(ctx, blockingGenLLM{}, "x"); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancelled, got %v", err)
	}
}

func TestOpenAIClientErrorBranches(t *testing.T) {
	if _, err := (&OpenAIClient{}).Generate(context.Background(), "x"); !errors.Is(err, ErrLLMUnavailable) {
		t.Fatalf("expected ErrLLMUnavailable, got %v", err)
	}
	badStatus := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer badStatus.Close()
	if _, err := (&OpenAIClient{APIKey: "k", BaseURL: badStatus.URL}).Generate(context.Background(), "x"); err == nil || !strings.Contains(err.Error(), "llm http status") {
		t.Fatalf("expected http status error, got %v", err)
	}
	noChoices := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"choices":[]}`))
	}))
	defer noChoices.Close()
	if _, err := (&OpenAIClient{APIKey: "k", BaseURL: noChoices.URL}).Generate(context.Background(), "x"); err == nil {
		t.Fatal("expected no choices error")
	}
	badJSON := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{bad json`))
	}))
	defer badJSON.Close()
	if _, err := (&OpenAIClient{APIKey: "k", BaseURL: badJSON.URL}).Generate(context.Background(), "x"); err == nil {
		t.Fatal("expected decode error")
	}
}

func TestLocalModelClientErrorBranches(t *testing.T) {
	badStatus := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer badStatus.Close()
	if _, err := (&LocalModelClient{BaseURL: badStatus.URL}).Generate(context.Background(), "x"); err == nil || !strings.Contains(err.Error(), "local llm http status") {
		t.Fatalf("expected http status error, got %v", err)
	}
	badJSON := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{bad json`))
	}))
	defer badJSON.Close()
	if _, err := (&LocalModelClient{BaseURL: badJSON.URL}).Generate(context.Background(), "x"); err == nil {
		t.Fatal("expected decode error")
	}
}

func TestLLMQualityScoreRegexFallback(t *testing.T) {
	results := []TestResult{{CaseID: "1", Status: "passed"}, {CaseID: "2", Status: "failed"}}
	// Sscanf fails on the leading text but regex still locates the number.
	e := NewAgentEngine(&mockLLM{response: "评分为 good 90 分"}, nil, nil, nil)
	if q := e.EvaluateQuality(results); q.CaseQualityScore != 90 {
		t.Fatalf("expected score 90, got %v", q.CaseQualityScore)
	}
	// No number anywhere: llmQualityScore returns false, deterministic baseline wins.
	e2 := NewAgentEngine(&mockLLM{response: "优秀，无数字"}, nil, nil, nil)
	if q := e2.EvaluateQuality(results); q.CaseQualityScore != 75 {
		t.Fatalf("expected deterministic 75, got %v", q.CaseQualityScore)
	}
}

func TestPersistenceEdgeBranches(t *testing.T) {
	empty := GORMAgentTaskStore{}
	if err := empty.SetStatus(context.Background(), "x", "running"); err == nil {
		t.Fatal("nil db SetStatus should fail")
	}
	if err := empty.CreateTask(context.Background(), TestTask{ID: "x"}, "queued"); err == nil {
		t.Fatal("nil db CreateTask should fail")
	}
	if err := empty.SaveResult(context.Background(), "x", OrchestrationResult{}, "failed", errors.New("boom")); err == nil {
		t.Fatal("nil db SaveResult should fail")
	}
	if _, _, err := empty.LoadResult(context.Background(), "x", 0); err == nil {
		t.Fatal("nil db LoadResult should fail")
	}
	if _, err := empty.ListResults(context.Background(), 0, 300); err == nil {
		t.Fatal("nil db ListResults should fail")
	}
	if err := empty.SaveKnowledge(context.Background(), Knowledge{}); err == nil {
		t.Fatal("empty knowledge should fail")
	}
	if _, err := empty.LoadKnowledge(context.Background(), 0); err == nil {
		t.Fatal("nil db LoadKnowledge should fail")
	}

	db, err := gorm.Open(sqlite.Open("file:agent_persist_cov?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := Migrate(db); err != nil {
		t.Fatal(err)
	}
	store := GORMAgentTaskStore{DB: db}
	// createdBy int branch + uint metadata project id
	task := TestTask{ID: "cov-1", Name: "cov", Description: "d", ProjectID: 5, Context: map[string]interface{}{"created_by": int(9)}}
	if err := store.CreateTask(context.Background(), task, "queued"); err != nil {
		t.Fatal(err)
	}
	// SaveResult with runErr writes error text
	result := OrchestrationResult{Task: task, TestCases: []TestCase{{ID: "TC-1", Title: "t"}}, Results: []TestResult{{CaseID: "TC-1", Status: "failed", StartedAt: time.Now(), FinishedAt: time.Now()}}, Quality: QualityReport{TotalCases: 1, ExecutedCases: 1, FailedCases: 1}}
	if err := store.SaveResult(context.Background(), task.ID, result, "failed", errors.New("run failed")); err != nil {
		t.Fatal(err)
	}
	loaded, status, err := store.LoadResult(context.Background(), task.ID, task.ProjectID)
	if err != nil || status != "failed" || len(loaded.Results) != 1 {
		t.Fatalf("loaded=%+v status=%s err=%v", loaded, status, err)
	}
	// ListResults limit>200 clamps to 50
	if list, err := store.ListResults(context.Background(), task.ProjectID, 300); err != nil || len(list) != 1 {
		t.Fatalf("list=%+v err=%v", list, err)
	}
	// metadataProjectID uint branch via SaveKnowledge
	k := Knowledge{Title: "规范", Type: "standard", Content: "加密", Metadata: map[string]interface{}{"project_id": uint(5)}}
	if err := store.SaveKnowledge(context.Background(), k); err != nil {
		t.Fatal(err)
	}
	items, err := store.LoadKnowledge(context.Background(), 5)
	if err != nil || len(items) != 1 {
		t.Fatalf("knowledge=%+v err=%v", items, err)
	}
}

func TestQueueSizeFloor(t *testing.T) {
	q := NewMemoryTaskQueue(0)
	if cap(q.ch) != 1 {
		t.Fatalf("expected channel cap 1, got %d", cap(q.ch))
	}
	if err := q.Enqueue(TestTask{ID: "floor"}); err != nil {
		t.Fatal(err)
	}
	got, err := q.Dequeue(context.Background())
	if err != nil || got.ID != "floor" {
		t.Fatalf("got=%+v err=%v", got, err)
	}
}

func TestRunWorkerDurableAck(t *testing.T) {
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatal(err)
	}
	defer mr.Close()
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	q := NewDurableRedisTaskQueue(client, "cov:worker")
	e := NewAgentEngine(nil, nil, q, nil)
	e.Config.Timeout = 2 * time.Second
	e.Config.Parallel = 1
	done := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go e.RunWorker(ctx, func(OrchestrationResult, error) { close(done) })
	if err := e.SubmitTask(TestTask{ID: "durable-1", Description: "durable worker"}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("durable worker did not process")
	}
	// onResult fires before the durable Ack; poll the processing list until empty.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if n, _ := client.LLen(context.Background(), q.ProcessingKey).Result(); n == 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	n, _ := client.LLen(context.Background(), q.ProcessingKey).Result()
	t.Fatalf("durable task not acked, processing len=%d", n)
}
