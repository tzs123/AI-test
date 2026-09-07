package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/runnergo/runnergo/backend/internal/agent"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

func doJSON(r http.Handler, method, path string, payload interface{}) *httptest.ResponseRecorder {
	var body *bytes.Reader
	if payload == nil {
		body = bytes.NewReader(nil)
	} else {
		raw, _ := json.Marshal(payload)
		body = bytes.NewReader(raw)
	}
	req := httptest.NewRequest(method, path, body)
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func newBatchDeleteRouter(t *testing.T, withAuth bool) (*gin.Engine, *gorm.DB) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	db, err := gorm.Open(sqlite.Open("file:batch-delete?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&agent.AgentTaskModel{}, &agent.AgentTestCaseModel{}, &agent.AgentTestResultModel{}); err != nil {
		t.Fatal(err)
	}
	h := NewAgentHandlerWithStores(nil, nil, nil, agent.GORMAgentTaskStore{DB: db})
	r := gin.New()
	if withAuth {
		r.Use(func(c *gin.Context) { c.Set("user_id", uint(1)); c.Next() })
	}
	h.RegisterRoutes(r)
	return r, db
}

func seedBatchDeleteData(t *testing.T, db *gorm.DB) {
	t.Helper()
	tasks := []agent.AgentTaskModel{
		{TaskID: "task-del-1", Name: "登录回归", Status: "completed", ProjectID: 1, CreatedAt: time.Now(), UpdatedAt: time.Now()},
		{TaskID: "task-del-2", Name: "支付回归", Status: "completed", ProjectID: 1, CreatedAt: time.Now(), UpdatedAt: time.Now()},
		{TaskID: "task-del-3", Name: "其他项目", Status: "completed", ProjectID: 2, CreatedAt: time.Now(), UpdatedAt: time.Now()},
	}
	for i := range tasks {
		if err := db.Create(&tasks[i]).Error; err != nil {
			t.Fatal(err)
		}
		cases := []agent.AgentTestCaseModel{{TaskID: tasks[i].ID, CaseID: "case-1", Title: "登录", StepsJSON: "[]", TagsJSON: "[]"}}
		results := []agent.AgentTestResultModel{{TaskID: tasks[i].ID, CaseID: "case-1", Status: "passed", DurationMS: 100}}
		for j := range cases {
			if err := db.Create(&cases[j]).Error; err != nil {
				t.Fatal(err)
			}
		}
		for j := range results {
			if err := db.Create(&results[j]).Error; err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestBatchDeleteTasks(t *testing.T) {
	r, db := newBatchDeleteRouter(t, true)
	seedBatchDeleteData(t, db)

	// Validation errors.
	if w := doJSON(r, http.MethodPost, "/agent/tasks/batch-delete", map[string]interface{}{}); w.Code != http.StatusBadRequest {
		t.Fatalf("empty body = %d", w.Code)
	}
	if w := doJSON(r, http.MethodPost, "/agent/tasks/batch-delete", map[string]interface{}{"task_ids": []string{}}); w.Code != http.StatusBadRequest {
		t.Fatalf("empty task_ids = %d", w.Code)
	}

	// Project scoping: project 1 tasks are deleted, project 2 survives.
	w := doJSON(r, http.MethodPost, "/agent/tasks/batch-delete?project_id=1", map[string]interface{}{
		"task_ids": []string{"task-del-1", "task-del-2", "task-del-3", "task-missing", " "},
	})
	if w.Code != http.StatusOK {
		t.Fatalf("delete = %d body=%s", w.Code, w.Body.String())
	}
	var out map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if deleted, _ := out["deleted"].(float64); deleted != 2 {
		t.Fatalf("deleted = %s", w.Body.String())
	}
	var remaining []agent.AgentTaskModel
	if err := db.Unscoped().Find(&remaining).Error; err != nil {
		t.Fatal(err)
	}
	if len(remaining) != 1 || remaining[0].TaskID != "task-del-3" {
		t.Fatalf("remaining tasks = %+v", remaining)
	}
	var cases, results int64
	if err := db.Model(&agent.AgentTestCaseModel{}).Count(&cases).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(&agent.AgentTestResultModel{}).Count(&results).Error; err != nil {
		t.Fatal(err)
	}
	if cases != 1 || results != 1 {
		t.Fatalf("orphan rows: cases=%d results=%d", cases, results)
	}
	// Listing no longer returns the deleted tasks; the survivor is project 2.
	list := doJSON(r, http.MethodGet, "/agent/tasks?project_id=2", nil)
	if list.Code != http.StatusOK {
		t.Fatalf("list = %d", list.Code)
	}
	var listed map[string]interface{}
	if err := json.Unmarshal(list.Body.Bytes(), &listed); err != nil {
		t.Fatal(err)
	}
	if total, _ := listed["total"].(float64); total != 1 {
		t.Fatalf("listed tasks = %s", list.Body.String())
	}
}

func TestBatchDeleteTasksUnauthenticated(t *testing.T) {
	r, _ := newBatchDeleteRouter(t, false)
	if w := doJSON(r, http.MethodPost, "/agent/tasks/batch-delete", map[string]interface{}{"task_ids": []string{"x"}}); w.Code != http.StatusUnauthorized {
		t.Fatalf("auth = %d", w.Code)
	}
}
