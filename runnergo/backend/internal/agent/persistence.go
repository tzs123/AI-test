package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"
)

// AgentTaskStore is the durable boundary between the workflow engine and the
// API. The in-memory status map remains available for backwards compatibility,
// while production deployments use this store for restart-safe history.
type AgentTaskStore interface {
	CreateTask(context.Context, TestTask, string) error
	SaveResult(context.Context, string, OrchestrationResult, string, error) error
	LoadResult(context.Context, string, uint) (OrchestrationResult, string, error)
	ListResults(context.Context, uint, uint) ([]OrchestrationResult, error)
	DeleteResults(context.Context, []string, uint) (int64, error)
	SaveKnowledge(context.Context, Knowledge) error
	LoadKnowledge(context.Context, uint) ([]Knowledge, error)
}

type GORMAgentTaskStore struct{ DB *gorm.DB }

func (s GORMAgentTaskStore) SetStatus(ctx context.Context, taskID, status string) error {
	if s.DB == nil {
		return errors.New("agent database is not configured")
	}
	return s.DB.WithContext(ctx).Model(&AgentTaskModel{}).Where("task_id = ?", taskID).Updates(map[string]interface{}{"status": status, "progress": progressForStatus(status), "updated_at": time.Now()}).Error
}

func (s GORMAgentTaskStore) CreateTask(ctx context.Context, task TestTask, status string) error {
	if s.DB == nil {
		return errors.New("agent database is not configured")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	b, err := json.Marshal(task.Context)
	if err != nil {
		return err
	}
	return s.DB.WithContext(ctx).Create(&AgentTaskModel{
		TaskID: task.ID, Name: task.Name, Description: task.Description,
		Status: status, Progress: 0, ProjectID: task.ProjectID,
		ContextJSON: string(b), ResultJSON: "{}", CreatedBy: createdBy(task), CreatedAt: time.Now(), UpdatedAt: time.Now(),
	}).Error
}

func (s GORMAgentTaskStore) SaveResult(ctx context.Context, taskID string, result OrchestrationResult, status string, runErr error) error {
	if s.DB == nil {
		return errors.New("agent database is not configured")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	return s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var task AgentTaskModel
		if err := tx.Where("task_id = ?", taskID).First(&task).Error; err != nil {
			return err
		}
		resultJSON, err := json.Marshal(result)
		if err != nil {
			return err
		}
		finished := time.Now()
		errText := ""
		if runErr != nil {
			errText = runErr.Error()
		}
		if err := tx.Model(&task).Updates(map[string]interface{}{
			"status": status, "progress": progressForStatus(status), "result_json": string(resultJSON),
			"error": errText, "finished_at": finished, "updated_at": finished,
		}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", task.ID).Delete(&AgentTestCaseModel{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", task.ID).Delete(&AgentTestResultModel{}).Error; err != nil {
			return err
		}
		for _, tc := range result.TestCases {
			steps, _ := json.Marshal(tc.Steps)
			tags, _ := json.Marshal(tc.Tags)
			metadata, _ := json.Marshal(tc.Metadata)
			if err := tx.Create(&AgentTestCaseModel{TaskID: task.ID, CaseID: tc.ID, Title: tc.Title,
				ScenarioType: tc.ScenarioType, Priority: tc.Priority, Description: tc.Description,
				StepsJSON: string(steps), ExpectedResult: tc.ExpectedResult, Script: tc.Script,
				TagsJSON: string(tags), MetadataJSON: string(metadata), CreatedAt: time.Now(), UpdatedAt: time.Now()}).Error; err != nil {
				return err
			}
		}
		for _, r := range result.Results {
			if err := tx.Create(&AgentTestResultModel{TaskID: task.ID, CaseID: r.CaseID, Status: r.Status,
				Expected: r.Expected, Actual: r.Actual, Logs: r.Logs, Error: r.Error,
				DurationMS: r.Duration.Milliseconds(), StartedAt: r.StartedAt, FinishedAt: r.FinishedAt}).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

func (s GORMAgentTaskStore) LoadResult(ctx context.Context, taskID string, projectID uint) (OrchestrationResult, string, error) {
	if s.DB == nil {
		return OrchestrationResult{}, "", errors.New("agent database is not configured")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	var task AgentTaskModel
	q := s.DB.WithContext(ctx).Where("task_id = ?", taskID)
	if projectID != 0 {
		q = q.Where("project_id = ?", projectID)
	}
	if err := q.First(&task).Error; err != nil {
		return OrchestrationResult{}, "", err
	}
	var result OrchestrationResult
	if task.ResultJSON != "" {
		_ = json.Unmarshal([]byte(task.ResultJSON), &result)
	}
	if result.Task.ID == "" {
		result.Task = TestTask{ID: task.TaskID, Name: task.Name, Description: task.Description, ProjectID: task.ProjectID}
		if task.ContextJSON != "" {
			_ = json.Unmarshal([]byte(task.ContextJSON), &result.Task.Context)
		}
	}
	var cases []AgentTestCaseModel
	_ = s.DB.WithContext(ctx).Where("task_id = ?", task.ID).Find(&cases).Error
	result.TestCases = make([]TestCase, 0, len(cases))
	for _, row := range cases {
		var tc TestCase
		tc.ID, tc.Title, tc.ScenarioType, tc.Priority = row.CaseID, row.Title, row.ScenarioType, row.Priority
		tc.Description, tc.ExpectedResult, tc.Script = row.Description, row.ExpectedResult, row.Script
		_ = json.Unmarshal([]byte(row.StepsJSON), &tc.Steps)
		_ = json.Unmarshal([]byte(row.TagsJSON), &tc.Tags)
		_ = json.Unmarshal([]byte(row.MetadataJSON), &tc.Metadata)
		result.TestCases = append(result.TestCases, tc)
	}
	var rows []AgentTestResultModel
	_ = s.DB.WithContext(ctx).Where("task_id = ?", task.ID).Order("id asc").Find(&rows).Error
	result.Results = make([]TestResult, 0, len(rows))
	for _, row := range rows {
		result.Results = append(result.Results, TestResult{CaseID: row.CaseID, Status: row.Status, Expected: row.Expected,
			Actual: row.Actual, Logs: row.Logs, Error: row.Error, Duration: time.Duration(row.DurationMS) * time.Millisecond,
			StartedAt: row.StartedAt, FinishedAt: row.FinishedAt})
	}
	return result, task.Status, nil
}

func (s GORMAgentTaskStore) ListResults(ctx context.Context, projectID, limit uint) ([]OrchestrationResult, error) {
	if s.DB == nil {
		return nil, errors.New("agent database is not configured")
	}
	if limit == 0 || limit > 200 {
		limit = 50
	}
	q := s.DB.WithContext(ctx).Order("id desc").Limit(int(limit))
	if projectID != 0 {
		q = q.Where("project_id = ?", projectID)
	}
	var tasks []AgentTaskModel
	if err := q.Find(&tasks).Error; err != nil {
		return nil, err
	}
	out := make([]OrchestrationResult, 0, len(tasks))
	for _, t := range tasks {
		result, _, err := s.LoadResult(ctx, t.TaskID, projectID)
		if err != nil {
			return nil, err
		}
		out = append(out, result)
	}
	return out, nil
}

// DeleteResults permanently removes the given agent tasks (matched by
// business task id, optionally scoped to a project) together with their
// persisted test cases and execution results. Tasks that do not exist or
// belong to another project are silently skipped; the return value is the
// number of task rows actually deleted.
func (s GORMAgentTaskStore) DeleteResults(ctx context.Context, taskIDs []string, projectID uint) (int64, error) {
	if s.DB == nil {
		return 0, errors.New("agent database is not configured")
	}
	trimmed := make([]string, 0, len(taskIDs))
	for _, id := range taskIDs {
		if id = strings.TrimSpace(id); id != "" {
			trimmed = append(trimmed, id)
		}
	}
	if len(trimmed) == 0 {
		return 0, errors.New("task_ids is required")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	var deleted int64
	err := s.DB.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		q := tx.Where("task_id IN ?", trimmed)
		if projectID != 0 {
			q = q.Where("project_id = ?", projectID)
		}
		var tasks []AgentTaskModel
		if err := q.Find(&tasks).Error; err != nil {
			return err
		}
		if len(tasks) == 0 {
			return nil
		}
		rowIDs := make([]uint, 0, len(tasks))
		for _, task := range tasks {
			rowIDs = append(rowIDs, task.ID)
		}
		if err := tx.Where("task_id IN ?", rowIDs).Delete(&AgentTestCaseModel{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id IN ?", rowIDs).Delete(&AgentTestResultModel{}).Error; err != nil {
			return err
		}
		result := tx.Unscoped().Where("id IN ?", rowIDs).Delete(&AgentTaskModel{})
		if result.Error != nil {
			return result.Error
		}
		deleted = result.RowsAffected
		return nil
	})
	return deleted, err
}

func (s GORMAgentTaskStore) SaveKnowledge(ctx context.Context, k Knowledge) error {
	if s.DB == nil {
		return errors.New("agent database is not configured")
	}
	if strings.TrimSpace(k.Title) == "" || strings.TrimSpace(k.Content) == "" {
		return errors.New("knowledge title and content are required")
	}
	meta, _ := json.Marshal(k.Metadata)
	return s.DB.WithContext(ctx).Create(&KnowledgeDocumentModel{Title: k.Title, Type: k.Type, Content: k.Content,
		Source: k.Source, ProjectID: metadataProjectID(k.Metadata), Metadata: string(meta), CreatedAt: time.Now(), UpdatedAt: time.Now()}).Error
}

func (s GORMAgentTaskStore) LoadKnowledge(ctx context.Context, projectID uint) ([]Knowledge, error) {
	if s.DB == nil {
		return nil, errors.New("agent database is not configured")
	}
	q := s.DB.WithContext(ctx).Order("id asc")
	if projectID != 0 {
		q = q.Where("project_id = 0 OR project_id = ?", projectID)
	}
	var rows []KnowledgeDocumentModel
	if err := q.Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]Knowledge, 0, len(rows))
	for _, row := range rows {
		var metadata map[string]interface{}
		_ = json.Unmarshal([]byte(row.Metadata), &metadata)
		out = append(out, Knowledge{ID: fmt.Sprint(row.ID), Title: row.Title, Type: row.Type, Content: row.Content, Source: row.Source, Metadata: metadata})
	}
	return out, nil
}

func createdBy(task TestTask) uint {
	if task.Context != nil {
		if value, ok := task.Context["created_by"]; ok {
			switch v := value.(type) {
			case int:
				return uint(v)
			case float64:
				return uint(v)
			}
		}
	}
	return 0
}

func progressForStatus(status string) int {
	switch strings.ToLower(status) {
	case "queued":
		return 5
	case "running":
		return 25
	case "completed", "passed", "failed":
		return 100
	default:
		return 0
	}
}

func metadataProjectID(m map[string]interface{}) uint {
	if m == nil {
		return 0
	}
	if v, ok := m["project_id"]; ok {
		switch x := v.(type) {
		case float64:
			return uint(x)
		case int:
			return uint(x)
		case uint:
			return x
		}
	}
	return 0
}
