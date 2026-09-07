package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/runnergo/runnergo/backend/internal/agent"
)

// AgentHandler exposes additive APIs; existing RunnerGo handlers and response
// envelopes are not changed.
type AgentHandler struct {
	Engine           *agent.AgentEngine
	Orchestrator     *agent.Orchestrator
	Authorize        func(*gin.Context) bool
	ExecutorStore    agent.ExecutorConfigStore
	TaskStore        agent.AgentTaskStore
	AuthorizeProject func(*gin.Context, uint) bool
	mu               sync.RWMutex
	statuses         map[string]agent.OrchestrationResult
}

func NewAgentHandler(engine *agent.AgentEngine, authorize func(*gin.Context) bool) *AgentHandler {
	return NewAgentHandlerWithExecutorStore(engine, authorize, nil)
}

func NewAgentHandlerWithExecutorStore(engine *agent.AgentEngine, authorize func(*gin.Context) bool, store agent.ExecutorConfigStore) *AgentHandler {
	return NewAgentHandlerWithStores(engine, authorize, store, nil)
}

func NewAgentHandlerWithStores(engine *agent.AgentEngine, authorize func(*gin.Context) bool, executorStore agent.ExecutorConfigStore, taskStore agent.AgentTaskStore) *AgentHandler {
	if engine == nil {
		engine = agent.NewAgentEngine(nil, nil, nil, nil)
	}
	return &AgentHandler{Engine: engine, Orchestrator: agent.NewOrchestrator(engine), Authorize: authorize, ExecutorStore: executorStore, TaskStore: taskStore, statuses: map[string]agent.OrchestrationResult{}}
}

func (h *AgentHandler) SetProjectAuthorizer(fn func(*gin.Context, uint) bool) {
	h.AuthorizeProject = fn
}

func (h *AgentHandler) projectAllowed(c *gin.Context, projectID uint) bool {
	return projectID == 0 || h.AuthorizeProject == nil || h.AuthorizeProject(c, projectID)
}

func (h *AgentHandler) authenticated(c *gin.Context) bool {
	if h.Authorize != nil {
		return h.Authorize(c)
	}
	// Existing auth middleware can expose any of these conventional keys.
	_, user := c.Get("user")
	_, uid := c.Get("user_id")
	_, claims := c.Get("claims")
	return user || uid || claims
}

func (h *AgentHandler) RegisterRoutes(r gin.IRouter) {
	r.POST("/agent/test-cases", h.GenerateTestCases)
	r.POST("/agent/execute", h.ExecuteTestCases)
	r.POST("/agent/run", h.Run)
	r.POST("/agent/runs", h.RunAsync)
	r.GET("/agent/tasks/:id", h.Status)
	r.GET("/agent/tasks", h.Tasks)
	r.POST("/agent/tasks/batch-delete", h.BatchDeleteTasks)
	r.GET("/agent/tasks/:id/events", h.Events)
	r.GET("/agent/capabilities", h.Capabilities)
	r.POST("/agent/knowledge/search", h.KnowledgeSearch)
	r.POST("/agent/knowledge/documents", h.KnowledgeDocument)
	r.POST("/agent/quality", h.Quality)
	r.GET("/agent/executor-config", h.GetExecutorConfig)
	r.PUT("/agent/executor-config", h.PutExecutorConfig)
}

// GetExecutorConfig godoc
// @Summary Get test executor configuration
// @Tags agent
// @Produce json
// @Param project_id query int false "project id"
// @Success 200 {object} map[string]interface{}
// @Failure 401 {object} map[string]string
// @Router /agent/executor-config [get]
func (h *AgentHandler) GetExecutorConfig(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	projectID := parseProjectID(c)
	if !h.projectAllowed(c, projectID) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	cfg := agent.ExecutorConfig{Type: "none", ProjectID: projectID}
	if h.Engine != nil {
		cfg = h.Engine.GetExecutorConfigForProject(projectID)
	}
	cfg.ProjectID = projectID
	if h.ExecutorStore != nil {
		loaded, err := h.ExecutorStore.Load(c.Request.Context(), projectID)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		cfg = loaded
	}
	if cfg.ProjectID == 0 {
		cfg.ProjectID = projectID
	}
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	c.JSON(http.StatusOK, gin.H{"config": redactedExecutorConfig(cfg)})
}

// PutExecutorConfig godoc
// @Summary Save and activate test executor configuration
// @Tags agent
// @Accept json
// @Produce json
// @Param config body agent.ExecutorConfig true "executor configuration"
// @Success 200 {object} map[string]interface{}
// @Failure 400 {object} map[string]string
// @Failure 401 {object} map[string]string
// @Router /agent/executor-config [put]
func (h *AgentHandler) PutExecutorConfig(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var cfg agent.ExecutorConfig
	if err := c.ShouldBindJSON(&cfg); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if cfg.ProjectID == 0 {
		cfg.ProjectID = parseProjectID(c)
	}
	if !h.projectAllowed(c, cfg.ProjectID) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	// An empty token on update preserves the stored credential because GET is
	// intentionally redacted and UIs cannot round-trip its original value.
	if h.ExecutorStore != nil && strings.TrimSpace(cfg.Token) == "" {
		if current, err := h.ExecutorStore.Load(c.Request.Context(), cfg.ProjectID); err == nil {
			cfg.Token = current.Token
			if _, present := headerValue(cfg.Headers, "Authorization"); !present {
				if value, ok := headerValue(current.Headers, "Authorization"); ok {
					if cfg.Headers == nil {
						cfg.Headers = map[string]string{}
					}
					cfg.Headers["Authorization"] = value
				}
			}
		}
	}
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	if h.Engine == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "agent engine is unavailable"})
		return
	}
	if err := agent.ValidateExecutorConfig(cfg); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if h.ExecutorStore != nil {
		saved, err := h.ExecutorStore.Save(c.Request.Context(), cfg)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		cfg = saved
	}
	if err := h.Engine.ApplyExecutorConfig(cfg); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	responseConfig := redactedExecutorConfig(h.Engine.GetExecutorConfig())
	c.JSON(http.StatusOK, gin.H{"config": responseConfig, "message": "executor configuration saved"})
}

func redactedExecutorConfig(cfg agent.ExecutorConfig) agent.ExecutorConfig {
	cfg.Token = ""
	if len(cfg.Headers) > 0 {
		headers := make(map[string]string, len(cfg.Headers))
		for key, value := range cfg.Headers {
			if strings.EqualFold(key, "Authorization") || strings.EqualFold(key, "Proxy-Authorization") {
				continue
			}
			headers[key] = value
		}
		cfg.Headers = headers
	}
	return cfg
}

func headerValue(headers map[string]string, name string) (string, bool) {
	for key, value := range headers {
		if strings.EqualFold(key, name) {
			return value, true
		}
	}
	return "", false
}

func parseProjectID(c *gin.Context) uint {
	var projectID uint
	if value := c.Query("project_id"); value != "" {
		if parsed, err := strconv.ParseUint(value, 10, 32); err == nil {
			projectID = uint(parsed)
		}
	}
	return projectID
}

// GenerateTestCases godoc
// @Summary Generate AI test cases
// @Tags agent
// @Accept json
// @Produce json
// @Param task body agent.TestTask true "test task"
// @Success 200 {object} map[string]interface{}
// @Failure 400 {object} map[string]string
// @Failure 401 {object} map[string]string
// @Router /agent/test-cases [post]
func (h *AgentHandler) GenerateTestCases(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var task agent.TestTask
	if err := c.ShouldBindJSON(&task); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	cases, err := h.Engine.GenerateTestCases(task)
	if len(cases) == 0 && err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	response := gin.H{"task": task, "test_cases": cases}
	if err != nil {
		response["warning"] = err.Error()
	}
	c.JSON(http.StatusOK, response)
}

// Capabilities godoc
// @Summary List Smart Test Engine capabilities
// @Tags agent
// @Produce json
// @Success 200 {object} map[string]interface{}
// @Router /agent/capabilities [get]
func (h *AgentHandler) Capabilities(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	queueLen := 0
	if h.Engine != nil && h.Engine.TaskQueue != nil {
		queueLen = h.Engine.TaskQueue.Len()
	}
	provider := "fallback"
	executor := agent.ExecutorConfig{Type: "none"}
	if h.Engine != nil && h.Engine.LLMClient != nil {
		provider = "configured"
	}
	if h.Engine != nil {
		executor = redactedExecutorConfig(h.Engine.GetExecutorConfig())
	}
	c.JSON(http.StatusOK, gin.H{"agents": []gin.H{{"name": "design-agent", "role": "测试设计Agent", "status": "ready"}, {"name": "execute-agent", "role": "执行Agent", "status": "ready"}, {"name": "evaluate-agent", "role": "评估Agent", "status": "ready"}}, "features": []string{"需求提取", "正向/边界/异常用例生成", "自动执行与断言比对", "历史缺陷知识检索", "质量报告"}, "llm_provider": provider, "queue_length": queueLen, "executor": gin.H{"enabled": executor.Enabled, "type": executor.Type}})
}

// Tasks godoc
// @Summary List in-memory Agent workflow tasks
// @Tags agent
// @Produce json
// @Success 200 {object} map[string]interface{}
// @Router /agent/tasks [get]
func (h *AgentHandler) Tasks(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	if h.TaskStore != nil {
		projectID := parseProjectID(c)
		if !h.projectAllowed(c, projectID) {
			c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
			return
		}
		items, err := h.TaskStore.ListResults(c.Request.Context(), projectID, 100)
		if err == nil {
			c.JSON(http.StatusOK, gin.H{"tasks": items, "total": len(items)})
			return
		}
	}
	h.mu.RLock()
	defer h.mu.RUnlock()
	items := make([]agent.OrchestrationResult, 0, len(h.statuses))
	for _, item := range h.statuses {
		items = append(items, item)
	}
	c.JSON(http.StatusOK, gin.H{"tasks": items, "total": len(items)})
}

// BatchDeleteTasks godoc
// @Summary Permanently delete multiple agent task records
// @Tags agent
// @Accept json
// @Produce json
// @Param request body batchDeleteTasksRequest true "task ids to delete"
// @Success 200 {object} map[string]interface{}
// @Failure 400 {object} map[string]string
// @Failure 401 {object} map[string]string
// @Router /agent/tasks/batch-delete [post]
func (h *AgentHandler) BatchDeleteTasks(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	if h.TaskStore == nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "task persistence is not configured"})
		return
	}
	var req batchDeleteTasksRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if len(req.TaskIDs) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "task_ids is required"})
		return
	}
	projectID := parseProjectID(c)
	if !h.projectAllowed(c, projectID) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	deleted, err := h.TaskStore.DeleteResults(c.Request.Context(), req.TaskIDs, projectID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"deleted": deleted})
}

type batchDeleteTasksRequest struct {
	TaskIDs []string `json:"task_ids"`
}

// KnowledgeSearch godoc
// @Summary Retrieve relevant knowledge
// @Tags agent
// @Accept json
// @Produce json
// @Param request body map[string]string true "context"
// @Success 200 {object} map[string]interface{}
// @Router /agent/knowledge/search [post]
func (h *AgentHandler) KnowledgeSearch(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var req struct {
		Context string `json:"context"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if h.Engine == nil || h.Engine.KnowledgeBase == nil {
		c.JSON(http.StatusOK, gin.H{"knowledge": []agent.Knowledge{}})
		return
	}
	if !h.projectAllowed(c, parseProjectID(c)) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	if h.TaskStore != nil {
		// Persisted documents are loaded on startup; this refreshes the in-memory
		// index for operators who add documents through another replica.
		if items, err := h.TaskStore.LoadKnowledge(c.Request.Context(), parseProjectID(c)); err == nil && len(items) > 0 {
			for _, item := range items {
				h.Engine.KnowledgeBase.Add(item)
			}
		}
	}
	c.JSON(http.StatusOK, gin.H{"knowledge": h.Engine.KnowledgeBase.Retrieve(req.Context)})
}

// KnowledgeDocument godoc
// @Summary Add a knowledge document
// @Tags agent
// @Accept json
// @Produce json
// @Param document body agent.Knowledge true "knowledge document"
// @Success 201 {object} map[string]interface{}
// @Router /agent/knowledge/documents [post]
func (h *AgentHandler) KnowledgeDocument(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var item agent.Knowledge
	if err := c.ShouldBindJSON(&item); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if item.Title == "" || item.Content == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "title and content are required"})
		return
	}
	if !h.projectAllowed(c, parseProjectID(c)) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	if projectID := parseProjectID(c); projectID != 0 {
		if item.Metadata == nil {
			item.Metadata = map[string]interface{}{}
		}
		item.Metadata["project_id"] = projectID
	}
	if h.Engine == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "agent engine is unavailable"})
		return
	}
	if h.Engine.KnowledgeBase == nil {
		h.Engine.KnowledgeBase = agent.NewKnowledgeBase()
	}
	h.Engine.KnowledgeBase.Add(item)
	if h.TaskStore != nil {
		if err := h.TaskStore.SaveKnowledge(c.Request.Context(), item); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
	}
	c.JSON(http.StatusCreated, gin.H{"knowledge": item, "message": "knowledge saved"})
}

// Quality godoc
// @Summary Evaluate test quality
// @Tags agent
// @Accept json
// @Produce json
// @Param request body map[string]interface{} true "results"
// @Success 200 {object} map[string]interface{}
// @Router /agent/quality [post]
func (h *AgentHandler) Quality(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var req struct {
		Results []agent.TestResult `json:"results"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	report := h.Engine.EvaluateQuality(req.Results)
	c.JSON(http.StatusOK, gin.H{"quality": report, "report": agent.RenderQualityReport(report)})
}

// ExecuteTestCases godoc
// @Summary Execute generated test cases
// @Tags agent
// @Accept json
// @Produce json
// @Param request body map[string]interface{} true "task and test cases"
// @Success 200 {object} map[string]interface{}
// @Router /agent/execute [post]
func (h *AgentHandler) ExecuteTestCases(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var req struct {
		Task      agent.TestTask   `json:"task"`
		TestCases []agent.TestCase `json:"test_cases"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if len(req.TestCases) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "test_cases is required"})
		return
	}
	var results []agent.TestResult
	var err error
	if req.Task.ProjectID != 0 {
		results, err = h.Engine.ExecuteTestCasesForProject(req.Task.ProjectID, req.TestCases)
	} else {
		results, err = h.Engine.ExecuteTestCases(req.TestCases)
	}
	statusCode := http.StatusOK
	if err != nil && len(results) == 0 {
		statusCode = http.StatusInternalServerError
	}
	c.JSON(statusCode, gin.H{"task": req.Task, "results": results, "error": errorText(err)})
}

// Run godoc
// @Summary Run the complete design/execute/evaluate workflow
// @Tags agent
// @Accept json
// @Produce json
// @Param task body agent.TestTask true "test task"
// @Success 202 {object} agent.OrchestrationResult
// @Router /agent/run [post]
func (h *AgentHandler) Run(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var task agent.TestTask
	if err := c.ShouldBindJSON(&task); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if task.ID == "" {
		task.ID = time.Now().UTC().Format("20060102T150405.000000000Z")
	}
	result, err := h.Orchestrator.Run(c.Request.Context(), task)
	if err != nil && len(result.TestCases) == 0 {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error(), "task": task})
		return
	}
	h.mu.Lock()
	h.statuses[task.ID] = result
	h.mu.Unlock()
	c.JSON(http.StatusAccepted, result)
}

// RunAsync queues a durable workflow. The legacy /run endpoint remains
// synchronous for compatibility; new clients should use this endpoint and
// follow /agent/tasks/:id or the SSE events stream.
func (h *AgentHandler) RunAsync(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	var task agent.TestTask
	if err := c.ShouldBindJSON(&task); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if strings.TrimSpace(task.Description) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "task description is required"})
		return
	}
	if task.ID == "" {
		task.ID = newTaskID()
	}
	if !h.projectAllowed(c, task.ProjectID) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	if task.Context == nil {
		task.Context = map[string]interface{}{}
	}
	if _, ok := task.Context["created_by"]; !ok {
		task.Context["created_by"] = userID(c)
	}
	if h.TaskStore != nil {
		if err := h.TaskStore.CreateTask(c.Request.Context(), task, "queued"); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
	}
	if !h.projectAllowed(c, parseProjectID(c)) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	if err := h.Engine.SubmitTask(task); err != nil {
		if h.TaskStore != nil {
			_ = h.TaskStore.SaveResult(c.Request.Context(), task.ID, agent.OrchestrationResult{Task: task}, "failed", err)
		}
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "task queue unavailable", "detail": err.Error(), "task": task})
		return
	}
	h.mu.Lock()
	h.statuses[task.ID] = agent.OrchestrationResult{Task: task, Steps: []string{"queued"}}
	h.mu.Unlock()
	c.JSON(http.StatusAccepted, gin.H{"task": task, "status": "queued", "message": "任务已进入 Agent 队列"})
}

// Status godoc
// @Summary Get Agent workflow status
// @Tags agent
// @Produce json
// @Param id path string true "task id"
// @Success 200 {object} agent.OrchestrationResult
// @Failure 404 {object} map[string]string
// @Router /agent/tasks/{id} [get]
func (h *AgentHandler) Status(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	if !h.projectAllowed(c, parseProjectID(c)) {
		c.JSON(http.StatusForbidden, gin.H{"error": "no access to project"})
		return
	}
	if h.TaskStore != nil {
		if result, taskStatus, err := h.TaskStore.LoadResult(c.Request.Context(), c.Param("id"), parseProjectID(c)); err == nil {
			c.JSON(http.StatusOK, gin.H{"status": taskStatus, "task": result.Task, "test_cases": result.TestCases, "results": result.Results, "quality": result.Quality, "steps": result.Steps})
			return
		}
	}
	h.mu.RLock()
	result, ok := h.statuses[c.Param("id")]
	h.mu.RUnlock()
	if !ok {
		c.JSON(http.StatusNotFound, gin.H{"error": "task not found"})
		return
	}
	c.JSON(http.StatusOK, result)
}

// RecordResult is called by the background worker after a durable task has
// finished. It updates both the compatibility memory cache and persistent DB.
func (h *AgentHandler) RecordResult(result agent.OrchestrationResult, runErr error) {
	status := "passed"
	if runErr != nil || result.Quality.FailedCases > 0 || (result.Quality.SkippedCases > 0 && result.Quality.ExecutedCases == 0) {
		status = "failed"
	}
	if len(result.Results) == 0 && runErr != nil {
		status = "failed"
	}
	h.mu.Lock()
	h.statuses[result.Task.ID] = result
	h.mu.Unlock()
	if h.TaskStore != nil {
		_ = h.TaskStore.SaveResult(context.Background(), result.Task.ID, result, status, runErr)
	}
}

func (h *AgentHandler) MarkRunning(task agent.TestTask) {
	h.mu.Lock()
	current := h.statuses[task.ID]
	current.Task = task
	current.Steps = []string{"running"}
	h.statuses[task.ID] = current
	h.mu.Unlock()
	if h.TaskStore != nil {
		if store, ok := h.TaskStore.(interface {
			SetStatus(context.Context, string, string) error
		}); ok {
			_ = store.SetStatus(context.Background(), task.ID, "running")
		}
	}
}

// Events provides a lightweight SSE stream for dashboards. It polls the
// durable store so updates remain visible when workers run in another pod.
func (h *AgentHandler) Events(c *gin.Context) {
	if !h.authenticated(c) {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
		return
	}
	if h.TaskStore == nil {
		c.JSON(http.StatusNotImplemented, gin.H{"error": "durable task store is not configured"})
		return
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	ctx := c.Request.Context()
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()
	for {
		result, status, err := h.TaskStore.LoadResult(ctx, c.Param("id"), parseProjectID(c))
		if err == nil {
			payload, _ := json.Marshal(gin.H{"status": status, "task": result.Task, "test_cases": result.TestCases, "results": result.Results, "quality": result.Quality, "steps": result.Steps})
			_, _ = c.Writer.Write([]byte("data: " + string(payload) + "\n\n"))
			c.Writer.Flush()
			if status == "completed" || status == "passed" || status == "failed" || status == "cancelled" {
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func userID(c *gin.Context) uint {
	for _, key := range []string{"user_id", "uid"} {
		if value, ok := c.Get(key); ok {
			switch v := value.(type) {
			case uint:
				return v
			case int:
				return uint(v)
			case float64:
				return uint(v)
			}
		}
	}
	return 0
}

var taskSequence uint64

func newTaskID() string {
	return fmt.Sprintf("agent-%d-%d", time.Now().UTC().UnixNano(), atomic.AddUint64(&taskSequence, 1))
}

func errorText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
