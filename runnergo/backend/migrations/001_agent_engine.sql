-- RunnerGo Smart Test Engine (MySQL 8+)
-- The Go service also runs the equivalent schema through agent.Migrate.
CREATE TABLE IF NOT EXISTS agent_tasks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id VARCHAR(100) NOT NULL,
  name VARCHAR(200) NOT NULL,
  description TEXT NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'created',
  progress INT NOT NULL DEFAULT 0,
  project_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_by BIGINT UNSIGNED NOT NULL DEFAULT 0,
  context_json JSON NULL,
  result_json JSON NULL,
  error TEXT NULL,
  finished_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  deleted_at DATETIME(6) NULL,
  KEY idx_agent_task_task_id (task_id), KEY idx_agent_task_status (status),
  KEY idx_agent_task_project (project_id), KEY idx_agent_task_creator (created_by), KEY idx_agent_task_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_test_cases (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id BIGINT UNSIGNED NOT NULL,
  case_id VARCHAR(100) NOT NULL,
  title VARCHAR(300) NOT NULL,
  scenario_type VARCHAR(32) NOT NULL,
  priority VARCHAR(16) NOT NULL DEFAULT 'P2',
  description TEXT NULL,
  steps_json JSON NULL,
  expected_result TEXT NOT NULL,
  script TEXT NULL,
  tags_json JSON NULL,
  metadata_json JSON NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_agent_case_task (task_id), KEY idx_agent_case_id (case_id), KEY idx_agent_case_scenario (scenario_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_test_results (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id BIGINT UNSIGNED NOT NULL,
  case_id VARCHAR(100) NOT NULL,
  status VARCHAR(16) NOT NULL,
  expected TEXT NULL,
  actual TEXT NULL,
  logs TEXT NULL,
  error TEXT NULL,
  duration_ms BIGINT NOT NULL DEFAULT 0,
  started_at DATETIME(6) NULL,
  finished_at DATETIME(6) NULL,
  KEY idx_agent_result_task (task_id), KEY idx_agent_result_case (case_id), KEY idx_agent_result_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_documents (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(300) NOT NULL,
  type VARCHAR(32) NOT NULL,
  content LONGTEXT NOT NULL,
  source VARCHAR(500) NULL,
  project_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
  metadata JSON NULL,
  created_by BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  deleted_at DATETIME(6) NULL,
  KEY idx_knowledge_title (title), KEY idx_knowledge_type (type), KEY idx_knowledge_project (project_id), KEY idx_knowledge_deleted (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  document_id BIGINT UNSIGNED NOT NULL,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  embedding JSON NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_knowledge_chunk_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_memories (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  task_id BIGINT UNSIGNED NOT NULL,
  type VARCHAR(32) NOT NULL,
  content TEXT NOT NULL,
  payload JSON NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_agent_memory_task (task_id), KEY idx_agent_memory_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_executor_configs (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  type VARCHAR(32) NOT NULL DEFAULT 'none',
  base_url VARCHAR(1000) NULL,
  method VARCHAR(16) NOT NULL DEFAULT 'GET',
  expected_status INT NOT NULL DEFAULT 200,
  timeout_ms BIGINT NOT NULL DEFAULT 30000,
  headers_json JSON NULL,
  token VARCHAR(1000) NULL,
  command VARCHAR(500) NULL,
  allowed_commands JSON NULL,
  created_by BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY idx_agent_executor_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
