package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/runnergo/runnergo/backend/internal/agent"
)

func TestAgentRoutesAuthAndRun(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	e := agent.NewAgentEngine(nil, nil, nil, nil)
	h := NewAgentHandler(e, func(c *gin.Context) bool { _, ok := c.Get("user_id"); return ok })
	h.RegisterRoutes(r)
	b, _ := json.Marshal(agent.TestTask{Description: "login"})
	req := httptest.NewRequest(http.MethodPost, "/agent/test-cases", bytes.NewReader(b))
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != 401 {
		t.Fatal(w.Code)
	}
	// Authenticated generation and malformed payload paths.
	rAuth := gin.New()
	rAuth.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	hAuth := NewAgentHandler(e, nil)
	hAuth.RegisterRoutes(rAuth)
	bad := httptest.NewRequest(http.MethodPost, "/agent/test-cases", bytes.NewBufferString("{"))
	bw := httptest.NewRecorder()
	rAuth.ServeHTTP(bw, bad)
	if bw.Code != 400 {
		t.Fatalf("bad payload=%d", bw.Code)
	}
	good := httptest.NewRequest(http.MethodPost, "/agent/test-cases", bytes.NewReader(b))
	good.Header.Set("Content-Type", "application/json")
	gw := httptest.NewRecorder()
	rAuth.ServeHTTP(gw, good)
	if gw.Code != 200 {
		t.Fatalf("generate=%d", gw.Code)
	}
	// Execute validation and successful execution paths.
	empty, _ := json.Marshal(map[string]interface{}{"task": agent.TestTask{Description: "x"}})
	er := httptest.NewRequest(http.MethodPost, "/agent/execute", bytes.NewReader(empty))
	er.Header.Set("Content-Type", "application/json")
	ew := httptest.NewRecorder()
	rAuth.ServeHTTP(ew, er)
	if ew.Code != 400 {
		t.Fatalf("empty execute=%d", ew.Code)
	}
	valid, _ := json.Marshal(map[string]interface{}{"task": agent.TestTask{Description: "x"}, "test_cases": []agent.TestCase{{ID: "1", ExpectedResult: "ok"}}})
	er = httptest.NewRequest(http.MethodPost, "/agent/execute", bytes.NewReader(valid))
	er.Header.Set("Content-Type", "application/json")
	ew = httptest.NewRecorder()
	rAuth.ServeHTTP(ew, er)
	if ew.Code != 200 {
		t.Fatalf("execute=%d body=%s", ew.Code, ew.Body.String())
	}
	e.Executor = failingExecutor{}
	er = httptest.NewRequest(http.MethodPost, "/agent/execute", bytes.NewReader(valid))
	er.Header.Set("Content-Type", "application/json")
	ew = httptest.NewRecorder()
	rAuth.ServeHTTP(ew, er)
	if ew.Code != 200 {
		t.Fatalf("execute error=%d", ew.Code)
	}
	e.Executor = nil
	req = httptest.NewRequest(http.MethodPost, "/agent/run", bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	// Inject auth through a small middleware, matching existing RunnerGo auth context.
	r2 := gin.New()
	r2.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	h.RegisterRoutes(r2)
	w = httptest.NewRecorder()
	r2.ServeHTTP(w, req)
	if w.Code != 202 {
		t.Fatalf("run status=%d body=%s", w.Code, w.Body.String())
	}
	var payload map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &payload); err != nil || payload["quality"] == nil {
		t.Fatal(w.Body.String())
	}
	emptyTask, _ := json.Marshal(agent.TestTask{})
	badRun := httptest.NewRequest(http.MethodPost, "/agent/run", bytes.NewReader(emptyTask))
	badRun.Header.Set("Content-Type", "application/json")
	badRunW := httptest.NewRecorder()
	r2.ServeHTTP(badRunW, badRun)
	if badRunW.Code != 502 {
		t.Fatalf("bad run=%d", badRunW.Code)
	}
	statusReq := httptest.NewRequest(http.MethodGet, "/agent/tasks/20260101T000000.000000000Z", nil)
	sw := httptest.NewRecorder()
	r2.ServeHTTP(sw, statusReq)
	if sw.Code != 404 {
		t.Fatal(sw.Code)
	}
}

type fakeTaskStore struct {
	mu    sync.Mutex
	items map[string]struct {
		result agent.OrchestrationResult
		status string
	}
	knowledge []agent.Knowledge
}

func newFakeTaskStore() *fakeTaskStore {
	return &fakeTaskStore{items: map[string]struct {
		result agent.OrchestrationResult
		status string
	}{}}
}
func (s *fakeTaskStore) CreateTask(_ context.Context, task agent.TestTask, status string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items[task.ID] = struct {
		result agent.OrchestrationResult
		status string
	}{result: agent.OrchestrationResult{Task: task}, status: status}
	return nil
}
func (s *fakeTaskStore) SetStatus(_ context.Context, id, status string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.items[id]
	if !ok {
		return fmt.Errorf("not found")
	}
	v.status = status
	s.items[id] = v
	return nil
}
func (s *fakeTaskStore) SaveResult(_ context.Context, id string, result agent.OrchestrationResult, status string, _ error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items[id] = struct {
		result agent.OrchestrationResult
		status string
	}{result: result, status: status}
	return nil
}
func (s *fakeTaskStore) LoadResult(_ context.Context, id string, _ uint) (agent.OrchestrationResult, string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.items[id]
	if !ok {
		return agent.OrchestrationResult{}, "", fmt.Errorf("not found")
	}
	return v.result, v.status, nil
}
func (s *fakeTaskStore) ListResults(_ context.Context, _ uint, _ uint) ([]agent.OrchestrationResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]agent.OrchestrationResult, 0, len(s.items))
	for _, v := range s.items {
		out = append(out, v.result)
	}
	return out, nil
}
func (s *fakeTaskStore) DeleteResults(_ context.Context, taskIDs []string, _ uint) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var deleted int64
	for _, id := range taskIDs {
		if _, ok := s.items[id]; ok {
			delete(s.items, id)
			deleted++
		}
	}
	return deleted, nil
}
func (s *fakeTaskStore) SaveKnowledge(_ context.Context, k agent.Knowledge) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.knowledge = append(s.knowledge, k)
	return nil
}
func (s *fakeTaskStore) LoadKnowledge(_ context.Context, _ uint) ([]agent.Knowledge, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]agent.Knowledge(nil), s.knowledge...), nil
}

func TestAsyncDurableWorkflowAndEvents(t *testing.T) {
	gin.SetMode(gin.TestMode)
	store := newFakeTaskStore()
	engine := agent.NewAgentEngine(nil, nil, nil, nil)
	h := NewAgentHandlerWithStores(engine, nil, nil, store)
	engine.OnTaskStart = h.MarkRunning
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user_id", uint(9)); c.Next() })
	h.RegisterRoutes(r)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { engine.RunWorker(ctx, h.RecordResult); close(done) }()
	req := httptest.NewRequest(http.MethodPost, "/agent/runs", bytes.NewBufferString(`{"name":"async","description":"login","project_id":3}`))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusAccepted {
		t.Fatalf("queue=%d body=%s", w.Code, w.Body.String())
	}
	var body map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	task := body["task"].(map[string]interface{})
	id := task["id"].(string)
	for i := 0; i < 30; i++ {
		time.Sleep(10 * time.Millisecond)
		_, status, _ := store.LoadResult(context.Background(), id, 3)
		if status == "passed" || status == "failed" {
			break
		}
	}
	statusReq := httptest.NewRequest(http.MethodGet, "/agent/tasks/"+id+"?project_id=3", nil)
	sw := httptest.NewRecorder()
	r.ServeHTTP(sw, statusReq)
	if sw.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", sw.Code, sw.Body.String())
	}
	listReq := httptest.NewRequest(http.MethodGet, "/agent/tasks?project_id=3", nil)
	lw := httptest.NewRecorder()
	r.ServeHTTP(lw, listReq)
	if lw.Code != http.StatusOK || !bytes.Contains(lw.Body.Bytes(), []byte(id)) {
		t.Fatalf("list=%d %s", lw.Code, lw.Body.String())
	}
	eventReq := httptest.NewRequest(http.MethodGet, "/agent/tasks/"+id+"/events?project_id=3", nil)
	ew := httptest.NewRecorder()
	r.ServeHTTP(ew, eventReq)
	if ew.Code != http.StatusOK || !bytes.Contains(ew.Body.Bytes(), []byte("data:")) {
		t.Fatalf("events=%d body=%s", ew.Code, ew.Body.String())
	}
	cancel()
	<-done
}

func TestAsyncWorkflowValidationAndQueueFailure(t *testing.T) {
	gin.SetMode(gin.TestMode)
	queue := agent.NewMemoryTaskQueue(1)
	engine := agent.NewAgentEngine(nil, nil, queue, nil)
	h := NewAgentHandler(engine, func(*gin.Context) bool { return true })
	r := gin.New()
	h.RegisterRoutes(r)
	for _, body := range []string{"{", `{"description":""}`} {
		req := httptest.NewRequest(http.MethodPost, "/agent/runs", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Fatalf("body=%s status=%d", body, w.Code)
		}
	}
	queue.Close()
	req := httptest.NewRequest(http.MethodPost, "/agent/runs", bytes.NewBufferString(`{"description":"x"}`))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("queue=%d %s", w.Code, w.Body.String())
	}
}

func TestProjectAuthorizationAndKnowledgePersistence(t *testing.T) {
	store := newFakeTaskStore()
	h := NewAgentHandlerWithStores(agent.NewAgentEngine(nil, nil, nil, nil), func(*gin.Context) bool { return true }, nil, store)
	h.SetProjectAuthorizer(func(*gin.Context, uint) bool { return false })
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	h.RegisterRoutes(r)
	req := httptest.NewRequest(http.MethodGet, "/agent/executor-config?project_id=8", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusForbidden {
		t.Fatalf("project=%d", w.Code)
	}
	// Remove project restriction for the persistence path.
	h.SetProjectAuthorizer(nil)
	req = httptest.NewRequest(http.MethodPost, "/agent/knowledge/documents?project_id=8", bytes.NewBufferString(`{"title":"rule","content":"phone login"}`))
	req.Header.Set("Content-Type", "application/json")
	w = httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != http.StatusCreated || len(store.knowledge) != 1 || store.knowledge[0].Metadata["project_id"] == nil {
		t.Fatalf("knowledge=%d %s", w.Code, w.Body.String())
	}
}

type failingExecutor struct{}

func (failingExecutor) Execute(_ context.Context, _ agent.TestCase) (agent.ExecutionOutput, error) {
	return agent.ExecutionOutput{}, fmt.Errorf("failure")
}

func TestStatusSuccessAndDefaultAuth(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user", "u"); c.Next() })
	h := NewAgentHandler(agent.NewAgentEngine(nil, nil, nil, nil), nil)
	h.RegisterRoutes(r)
	task := agent.TestTask{ID: "known", Description: "x"}
	h.mu.Lock()
	h.statuses[task.ID] = agent.OrchestrationResult{Task: task}
	h.mu.Unlock()
	w := httptest.NewRecorder()
	r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/agent/tasks/known", nil))
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
	// Default auth rejects requests without a context user.
	rNoAuth := gin.New()
	hNoAuth := NewAgentHandler(agent.NewAgentEngine(nil, nil, nil, nil), nil)
	hNoAuth.RegisterRoutes(rNoAuth)
	nw := httptest.NewRecorder()
	rNoAuth.ServeHTTP(nw, httptest.NewRequest(http.MethodGet, "/agent/tasks/known", nil))
	if nw.Code != 401 {
		t.Fatal(nw.Code)
	}
}

func TestAgentAuxiliaryRoutes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("claims", map[string]interface{}{"sub": "1"}); c.Next() })
	e := agent.NewAgentEngine(nil, nil, nil, nil)
	h := NewAgentHandler(e, nil)
	h.RegisterRoutes(r)

	request := func(method, path, body string) *httptest.ResponseRecorder {
		var reader *bytes.Reader
		if body == "" {
			reader = bytes.NewReader(nil)
		} else {
			reader = bytes.NewReader([]byte(body))
		}
		req := httptest.NewRequest(method, path, reader)
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}

	if w := request(http.MethodGet, "/agent/capabilities", ""); w.Code != http.StatusOK {
		t.Fatalf("capabilities=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodGet, "/agent/tasks", ""); w.Code != http.StatusOK {
		t.Fatalf("tasks=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodPost, "/agent/knowledge/search", "{"); w.Code != http.StatusBadRequest {
		t.Fatalf("malformed knowledge search=%d", w.Code)
	}
	if w := request(http.MethodPost, "/agent/knowledge/search", `{"context":"login"}`); w.Code != http.StatusOK {
		t.Fatalf("knowledge search=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodPost, "/agent/knowledge/documents", `{"title":"","type":"document","content":"x"}`); w.Code != http.StatusBadRequest {
		t.Fatalf("invalid knowledge document=%d", w.Code)
	}
	if w := request(http.MethodPost, "/agent/knowledge/documents", `{"title":"Login rule","type":"document","content":"Users must login with a phone number"}`); w.Code != http.StatusCreated {
		t.Fatalf("add knowledge=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodPost, "/agent/knowledge/search", `{"context":"phone login"}`); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte("Login rule")) {
		t.Fatalf("knowledge retrieval=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodPost, "/agent/quality", "{"); w.Code != http.StatusBadRequest {
		t.Fatalf("malformed quality=%d", w.Code)
	}
	qualityBody := `{"results":[{"case_id":"TC-1","status":"passed","expected":"ok","actual":"ok"},{"case_id":"TC-2","status":"failed","expected":"ok","actual":"bad"}]}`
	if w := request(http.MethodPost, "/agent/quality", qualityBody); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte("quality")) {
		t.Fatalf("quality=%d body=%s", w.Code, w.Body.String())
	}

	// The list endpoint should include the workflow created by /agent/run.
	runBody := `{"id":"aux-task","description":"login"}`
	if w := request(http.MethodPost, "/agent/run", runBody); w.Code != http.StatusAccepted {
		t.Fatalf("run=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodGet, "/agent/tasks", ""); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte("aux-task")) {
		t.Fatalf("task list after run=%d body=%s", w.Code, w.Body.String())
	}
}

func TestAuxiliaryRoutesAuthenticationAndUnavailableBranches(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := NewAgentHandler(agent.NewAgentEngine(nil, nil, nil, nil), nil)
	unauth := gin.New()
	h.RegisterRoutes(unauth)
	paths := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/agent/capabilities", ""},
		{http.MethodGet, "/agent/tasks", ""},
		{http.MethodPost, "/agent/knowledge/search", `{"context":"x"}`},
		{http.MethodPost, "/agent/knowledge/documents", `{"title":"x","content":"y"}`},
		{http.MethodPost, "/agent/quality", `{"results":[]}`},
		{http.MethodPost, "/agent/execute", `{"test_cases":[]}`},
		{http.MethodPost, "/agent/run", `{"description":"x"}`},
	}
	for _, item := range paths {
		var body *bytes.Reader
		if item.body == "" {
			body = bytes.NewReader(nil)
		} else {
			body = bytes.NewReader([]byte(item.body))
		}
		req := httptest.NewRequest(item.method, item.path, body)
		w := httptest.NewRecorder()
		unauth.ServeHTTP(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s status=%d", item.method, item.path, w.Code)
		}
	}

	auth := gin.New()
	auth.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	h.RegisterRoutes(auth)
	h.Engine.KnowledgeBase = nil
	search := httptest.NewRequest(http.MethodPost, "/agent/knowledge/search", bytes.NewBufferString(`{"context":"x"}`))
	search.Header.Set("Content-Type", "application/json")
	sw := httptest.NewRecorder()
	auth.ServeHTTP(sw, search)
	if sw.Code != http.StatusOK || !bytes.Contains(sw.Body.Bytes(), []byte(`"knowledge":[]`)) {
		t.Fatalf("nil knowledge base status=%d body=%s", sw.Code, sw.Body.String())
	}
	h.Engine = nil
	doc := httptest.NewRequest(http.MethodPost, "/agent/knowledge/documents", bytes.NewBufferString(`{"title":"x","content":"y"}`))
	doc.Header.Set("Content-Type", "application/json")
	dw := httptest.NewRecorder()
	auth.ServeHTTP(dw, doc)
	if dw.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil engine document status=%d body=%s", dw.Code, dw.Body.String())
	}
}

func TestExecutorConfigRoutes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	store := agent.NewMemoryExecutorConfigStore()
	h := NewAgentHandlerWithExecutorStore(agent.NewAgentEngine(nil, nil, nil, nil), nil, store)
	h.RegisterRoutes(r)
	request := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, bytes.NewBufferString(body))
		if body != "" {
			req.Header.Set("Content-Type", "application/json")
		}
		w := httptest.NewRecorder()
		r.ServeHTTP(w, req)
		return w
	}
	if w := request(http.MethodGet, "/agent/executor-config?project_id=9", ""); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte(`"type":"none"`)) {
		t.Fatalf("get default=%d body=%s", w.Code, w.Body.String())
	}
	if w := request(http.MethodPut, "/agent/executor-config?project_id=9", "{"); w.Code != http.StatusBadRequest {
		t.Fatalf("malformed put=%d", w.Code)
	}
	if w := request(http.MethodPut, "/agent/executor-config?project_id=9", `{"enabled":true,"type":"api","base_url":"file:///tmp"}`); w.Code != http.StatusBadRequest {
		t.Fatalf("unsafe config=%d body=%s", w.Code, w.Body.String())
	}
	body := `{"enabled":true,"type":"command","token":"secret","headers":{"Authorization":"Basic hidden","X-Test":"yes"},"command":"printf ok","allowed_commands":["printf"]}`
	if w := request(http.MethodPut, "/agent/executor-config?project_id=9", body); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte(`"type":"command"`)) || bytes.Contains(w.Body.Bytes(), []byte("secret")) {
		t.Fatalf("put=%d body=%s", w.Code, w.Body.String())
	}
	if !h.Engine.ExecutorConfigured() {
		t.Fatal("executor not activated")
	}
	if w := request(http.MethodGet, "/agent/executor-config?project_id=9", ""); w.Code != http.StatusOK || !bytes.Contains(w.Body.Bytes(), []byte(`"project_id":9`)) || bytes.Contains(w.Body.Bytes(), []byte("hidden")) {
		t.Fatalf("get saved=%d body=%s", w.Code, w.Body.String())
	}
	withoutToken := `{"project_id":9,"enabled":true,"type":"command","command":"printf ok","allowed_commands":["printf"]}`
	if w := request(http.MethodPut, "/agent/executor-config", withoutToken); w.Code != http.StatusOK {
		t.Fatalf("preserve token update=%d body=%s", w.Code, w.Body.String())
	}
	if saved, _ := store.Load(context.Background(), 9); saved.Token != "secret" {
		t.Fatalf("token not preserved=%q", saved.Token)
	}
	if saved, _ := store.Load(context.Background(), 9); saved.Headers["Authorization"] != "Basic hidden" {
		t.Fatalf("authorization not preserved=%v", saved.Headers)
	}
	if w := request(http.MethodPut, "/agent/executor-config", `{"enabled":false,"type":"none"}`); w.Code != http.StatusOK || h.Engine.ExecutorConfigured() {
		t.Fatalf("disable=%d configured=%v", w.Code, h.Engine.ExecutorConfigured())
	}
}
