package agent

import (
	"time"

	"gorm.io/gorm"
)

// AgentTaskModel and related models are deliberately separate from runtime
// structs. This lets existing RunnerGo response schemas remain unchanged.
type AgentTaskModel struct {
	ID          uint           `gorm:"primaryKey" json:"id"`
	TaskID      string         `gorm:"size:100;index" json:"task_id"`
	Name        string         `gorm:"size:200" json:"name"`
	Description string         `gorm:"type:text" json:"description"`
	Status      string         `gorm:"size:32;index" json:"status"`
	Progress    int            `json:"progress"`
	ProjectID   uint           `gorm:"index" json:"project_id"`
	CreatedBy   uint           `gorm:"index" json:"created_by"`
	ContextJSON string         `gorm:"type:json" json:"-"`
	ResultJSON  string         `gorm:"type:json" json:"-"`
	Error       string         `gorm:"type:text" json:"-"`
	FinishedAt  *time.Time     `json:"finished_at,omitempty"`
	CreatedAt   time.Time      `json:"created_at"`
	UpdatedAt   time.Time      `json:"updated_at"`
	DeletedAt   gorm.DeletedAt `gorm:"index" json:"-"`
}

type AgentTestCaseModel struct {
	ID             uint      `gorm:"primaryKey" json:"id"`
	TaskID         uint      `gorm:"index" json:"task_id"`
	CaseID         string    `gorm:"size:100" json:"case_id"`
	Title          string    `gorm:"size:300" json:"title"`
	ScenarioType   string    `gorm:"size:32;index" json:"scenario_type"`
	Priority       string    `gorm:"size:16" json:"priority"`
	Description    string    `gorm:"type:text" json:"-"`
	StepsJSON      string    `gorm:"type:json" json:"-"`
	ExpectedResult string    `gorm:"type:text" json:"expected_result"`
	Script         string    `gorm:"type:text" json:"-"`
	TagsJSON       string    `gorm:"type:json" json:"-"`
	MetadataJSON   string    `gorm:"type:json" json:"-"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type AgentTestResultModel struct {
	ID         uint      `gorm:"primaryKey" json:"id"`
	TaskID     uint      `gorm:"index" json:"task_id"`
	CaseID     string    `gorm:"size:100;index" json:"case_id"`
	Status     string    `gorm:"size:16;index" json:"status"`
	Expected   string    `gorm:"type:text" json:"expected"`
	Actual     string    `gorm:"type:text" json:"actual"`
	Logs       string    `gorm:"type:text" json:"logs"`
	Error      string    `gorm:"type:text" json:"error"`
	DurationMS int64     `json:"duration_ms"`
	StartedAt  time.Time `json:"started_at"`
	FinishedAt time.Time `json:"finished_at"`
}

type KnowledgeDocumentModel struct {
	ID        uint           `gorm:"primaryKey" json:"id"`
	Title     string         `gorm:"size:300;index" json:"title"`
	Type      string         `gorm:"size:32;index" json:"type"`
	Content   string         `gorm:"type:longtext" json:"content"`
	Source    string         `gorm:"size:500" json:"source"`
	ProjectID uint           `gorm:"index" json:"project_id"`
	Metadata  string         `gorm:"type:json" json:"-"`
	CreatedBy uint           `gorm:"index" json:"created_by"`
	CreatedAt time.Time      `json:"created_at"`
	UpdatedAt time.Time      `json:"updated_at"`
	DeletedAt gorm.DeletedAt `gorm:"index" json:"-"`
}

type KnowledgeChunkModel struct {
	ID         uint      `gorm:"primaryKey" json:"id"`
	DocumentID uint      `gorm:"index" json:"document_id"`
	ChunkIndex int       `json:"chunk_index"`
	Content    string    `gorm:"type:text" json:"content"`
	Embedding  string    `gorm:"type:json" json:"-"`
	CreatedAt  time.Time `json:"created_at"`
}

type AgentMemoryModel struct {
	ID        uint      `gorm:"primaryKey" json:"id"`
	TaskID    uint      `gorm:"index" json:"task_id"`
	Type      string    `gorm:"size:32;index" json:"type"`
	Content   string    `gorm:"type:text" json:"content"`
	Payload   string    `gorm:"type:json" json:"-"`
	CreatedAt time.Time `json:"created_at"`
}

// AgentExecutorConfigModel stores one executor configuration per project.
// Secrets should be supplied through headers only in trusted deployments; the
// API never logs header values.
type AgentExecutorConfigModel struct {
	ID              uint      `gorm:"primaryKey" json:"id"`
	ProjectID       uint      `gorm:"uniqueIndex" json:"project_id"`
	Enabled         bool      `json:"enabled"`
	Type            string    `gorm:"size:32" json:"type"`
	BaseURL         string    `gorm:"size:1000" json:"base_url"`
	Method          string    `gorm:"size:16" json:"method"`
	ExpectedStatus  int       `json:"expected_status"`
	TimeoutMS       int64     `json:"timeout_ms"`
	HeadersJSON     string    `gorm:"type:json" json:"-"`
	Token           string    `gorm:"size:1000" json:"-"`
	Command         string    `gorm:"size:500" json:"command"`
	AllowedCommands string    `gorm:"type:json" json:"-"`
	CreatedBy       uint      `gorm:"index" json:"created_by"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

func (AgentTaskModel) TableName() string           { return "agent_tasks" }
func (AgentTestCaseModel) TableName() string       { return "agent_test_cases" }
func (AgentTestResultModel) TableName() string     { return "agent_test_results" }
func (KnowledgeDocumentModel) TableName() string   { return "knowledge_documents" }
func (KnowledgeChunkModel) TableName() string      { return "knowledge_chunks" }
func (AgentMemoryModel) TableName() string         { return "agent_memories" }
func (AgentExecutorConfigModel) TableName() string { return "agent_executor_configs" }

type Migrator interface {
	AutoMigrate(dst ...interface{}) error
}

func Migrate(db Migrator) error {
	if db == nil {
		return nil
	}
	return db.AutoMigrate(&AgentTaskModel{}, &AgentTestCaseModel{}, &AgentTestResultModel{}, &KnowledgeDocumentModel{}, &KnowledgeChunkModel{}, &AgentMemoryModel{})
}

func MigrateExecutorConfig(db Migrator) error {
	if db == nil {
		return nil
	}
	return db.AutoMigrate(&AgentExecutorConfigModel{})
}
