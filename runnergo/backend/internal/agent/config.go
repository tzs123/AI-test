package agent

import (
	"encoding/json"
	"os"
	"strconv"
	"strings"
	"time"
)

func ConfigFromEnv() EngineConfig {
	c := EngineConfig{Timeout: 60 * time.Second, LLMRetries: 2, RetryBackoff: 100 * time.Millisecond, Parallel: 1}
	if v, err := time.ParseDuration(os.Getenv("RUNNERGO_AGENT_TIMEOUT")); err == nil && v > 0 {
		c.Timeout = v
	}
	if v, err := strconv.Atoi(os.Getenv("RUNNERGO_AGENT_RETRIES")); err == nil && v >= 0 {
		c.LLMRetries = v
	}
	if v, err := time.ParseDuration(os.Getenv("RUNNERGO_AGENT_RETRY_BACKOFF")); err == nil && v >= 0 {
		c.RetryBackoff = v
	}
	if v, err := strconv.Atoi(os.Getenv("RUNNERGO_AGENT_PARALLEL")); err == nil && v > 0 {
		c.Parallel = v
	}
	return c
}

func ProviderFromEnv() string {
	return strings.ToLower(strings.TrimSpace(os.Getenv("RUNNERGO_LLM_PROVIDER")))
}

func NewEngineFromEnv(kb *KnowledgeBase, queue TaskQueue, collector ResultCollector) *AgentEngine {
	var llm LLMClient
	switch ProviderFromEnv() {
	case "openai":
		llm = &OpenAIClient{APIKey: os.Getenv("RUNNERGO_LLM_API_KEY"), Model: os.Getenv("RUNNERGO_LLM_MODEL"), BaseURL: os.Getenv("RUNNERGO_LLM_BASE_URL")}
	case "local", "ollama":
		llm = &LocalModelClient{Model: os.Getenv("RUNNERGO_LLM_MODEL"), BaseURL: os.Getenv("RUNNERGO_LLM_BASE_URL")}
	default:
		llm = nil
	}
	e := NewAgentEngine(llm, kb, queue, collector)
	e.Config = ConfigFromEnv()
	if cfg := ExecutorConfigFromEnv(); cfg.Enabled || (strings.ToLower(cfg.Type) == "none" && os.Getenv("RUNNERGO_EXECUTOR_TYPE") != "") {
		if err := e.ApplyExecutorConfig(cfg); err != nil {
			// Keep the engine available in fallback mode when an optional env
			// setting is malformed; the configuration API can correct it later.
			e.ExecutorConfig = ExecutorConfig{Type: "none"}
		}
	}
	return e
}

func ExecutorConfigFromEnv() ExecutorConfig {
	cfg := ExecutorConfig{Type: "none", Method: "GET", TimeoutMS: 30000}
	cfg.Type = strings.ToLower(strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_TYPE")))
	if cfg.Type == "" {
		cfg.Type = "none"
	}
	cfg.Enabled = strings.EqualFold(strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_ENABLED")), "true")
	cfg.BaseURL = strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_BASE_URL"))
	cfg.Token = strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_TOKEN"))
	if v := strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_METHOD")); v != "" {
		cfg.Method = v
	}
	if v, err := strconv.Atoi(os.Getenv("RUNNERGO_EXECUTOR_EXPECTED_STATUS")); err == nil && v >= 0 {
		cfg.ExpectedStatus = v
	}
	if v, err := strconv.ParseInt(os.Getenv("RUNNERGO_EXECUTOR_TIMEOUT_MS"), 10, 64); err == nil && v >= 0 {
		cfg.TimeoutMS = v
	}
	cfg.Command = strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_COMMAND"))
	if raw := strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_HEADERS")); raw != "" {
		_ = json.Unmarshal([]byte(raw), &cfg.Headers)
	}
	if raw := strings.TrimSpace(os.Getenv("RUNNERGO_EXECUTOR_ALLOWED_COMMANDS")); raw != "" {
		if json.Unmarshal([]byte(raw), &cfg.AllowedCommands) != nil {
			for _, item := range strings.Split(raw, ",") {
				if item = strings.TrimSpace(item); item != "" {
					cfg.AllowedCommands = append(cfg.AllowedCommands, item)
				}
			}
		}
	}
	return cfg
}
