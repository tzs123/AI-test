package agent

import (
	"context"
	"encoding/json"
	"errors"
	"sync"

	"gorm.io/gorm"
)

// GORMExecutorConfigStore persists project executor settings in MySQL.
type GORMExecutorConfigStore struct{ DB *gorm.DB }

func (s GORMExecutorConfigStore) LoadAll(ctx context.Context) ([]ExecutorConfig, error) {
	if s.DB == nil {
		return nil, errors.New("mysql db is nil")
	}
	var rows []AgentExecutorConfigModel
	if err := s.DB.WithContext(ctx).Order("project_id asc").Find(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]ExecutorConfig, 0, len(rows))
	for _, row := range rows {
		cfg, err := executorConfigFromModel(row)
		if err != nil {
			return nil, err
		}
		out = append(out, cfg)
	}
	return out, nil
}

func (s GORMExecutorConfigStore) Load(ctx context.Context, projectID uint) (ExecutorConfig, error) {
	if s.DB == nil {
		return ExecutorConfig{}, errors.New("mysql db is nil")
	}
	var row AgentExecutorConfigModel
	err := s.DB.WithContext(ctx).Where("project_id = ?", projectID).First(&row).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return ExecutorConfig{ProjectID: projectID, Type: "none"}, nil
	}
	if err != nil {
		return ExecutorConfig{}, err
	}
	return executorConfigFromModel(row)
}

func (s GORMExecutorConfigStore) Save(ctx context.Context, cfg ExecutorConfig) (ExecutorConfig, error) {
	if s.DB == nil {
		return ExecutorConfig{}, errors.New("mysql db is nil")
	}
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	if err := ValidateExecutorConfig(cfg); err != nil {
		return ExecutorConfig{}, err
	}
	row, err := executorConfigToModel(cfg)
	if err != nil {
		return ExecutorConfig{}, err
	}
	var existing AgentExecutorConfigModel
	err = s.DB.WithContext(ctx).Where("project_id = ?", cfg.ProjectID).First(&existing).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		if err = s.DB.WithContext(ctx).Create(&row).Error; err != nil {
			return ExecutorConfig{}, err
		}
		return executorConfigFromModel(row)
	}
	if err != nil {
		return ExecutorConfig{}, err
	}
	row.ID = existing.ID
	if err = s.DB.WithContext(ctx).Save(&row).Error; err != nil {
		return ExecutorConfig{}, err
	}
	return executorConfigFromModel(row)
}

func executorConfigToModel(cfg ExecutorConfig) (AgentExecutorConfigModel, error) {
	headers, err := json.Marshal(cfg.Headers)
	if err != nil {
		return AgentExecutorConfigModel{}, err
	}
	allowed, err := json.Marshal(cfg.AllowedCommands)
	if err != nil {
		return AgentExecutorConfigModel{}, err
	}
	return AgentExecutorConfigModel{ID: cfg.ID, ProjectID: cfg.ProjectID, Enabled: cfg.Enabled, Type: cfg.Type, BaseURL: cfg.BaseURL, Method: cfg.Method, ExpectedStatus: cfg.ExpectedStatus, TimeoutMS: cfg.TimeoutMS, HeadersJSON: string(headers), Token: cfg.Token, Command: cfg.Command, AllowedCommands: string(allowed), CreatedBy: cfg.CreatedBy, UpdatedAt: cfg.UpdatedAt}, nil
}

func executorConfigFromModel(row AgentExecutorConfigModel) (ExecutorConfig, error) {
	cfg := ExecutorConfig{ID: row.ID, ProjectID: row.ProjectID, Enabled: row.Enabled, Type: row.Type, BaseURL: row.BaseURL, Method: row.Method, ExpectedStatus: row.ExpectedStatus, TimeoutMS: row.TimeoutMS, Token: row.Token, Command: row.Command, CreatedBy: row.CreatedBy, UpdatedAt: row.UpdatedAt}
	if row.HeadersJSON != "" {
		if err := json.Unmarshal([]byte(row.HeadersJSON), &cfg.Headers); err != nil {
			return ExecutorConfig{}, err
		}
	}
	if row.AllowedCommands != "" {
		if err := json.Unmarshal([]byte(row.AllowedCommands), &cfg.AllowedCommands); err != nil {
			return ExecutorConfig{}, err
		}
	}
	return cfg, nil
}

// MemoryExecutorConfigStore is used when MySQL is unavailable and in tests.
type MemoryExecutorConfigStore struct {
	mu    sync.RWMutex
	items map[uint]ExecutorConfig
}

func NewMemoryExecutorConfigStore() *MemoryExecutorConfigStore {
	return &MemoryExecutorConfigStore{items: map[uint]ExecutorConfig{}}
}
func (s *MemoryExecutorConfigStore) Load(_ context.Context, projectID uint) (ExecutorConfig, error) {
	if s == nil {
		return ExecutorConfig{}, errors.New("executor config store is nil")
	}
	s.mu.RLock()
	cfg, ok := s.items[projectID]
	s.mu.RUnlock()
	if !ok {
		return ExecutorConfig{ProjectID: projectID, Type: "none"}, nil
	}
	return cfg, nil
}
func (s *MemoryExecutorConfigStore) Save(_ context.Context, cfg ExecutorConfig) (ExecutorConfig, error) {
	if s == nil {
		return ExecutorConfig{}, errors.New("executor config store is nil")
	}
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	if err := ValidateExecutorConfig(cfg); err != nil {
		return ExecutorConfig{}, err
	}
	s.mu.Lock()
	if s.items == nil {
		s.items = map[uint]ExecutorConfig{}
	}
	s.items[cfg.ProjectID] = cfg
	s.mu.Unlock()
	return cfg, nil
}
