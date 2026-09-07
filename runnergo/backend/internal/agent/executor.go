package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// CommandExecutor is an explicit opt-in script executor. It does not invoke a
// shell and only runs allow-listed binaries, preventing shell expansion and
// command chaining from API input.
type CommandExecutor struct {
	AllowedCommands map[string]bool
	WorkingDir      string
}

type ConfiguredCommandExecutor struct {
	CommandExecutor
	DefaultCommand string
}

func (e ConfiguredCommandExecutor) Execute(ctx context.Context, tc TestCase) (ExecutionOutput, error) {
	if strings.TrimSpace(tc.Script) == "" {
		tc.Script = e.DefaultCommand
	}
	return e.CommandExecutor.Execute(ctx, tc)
}

func (e CommandExecutor) Execute(ctx context.Context, tc TestCase) (ExecutionOutput, error) {
	fields := strings.Fields(strings.TrimSpace(tc.Script))
	if len(fields) == 0 {
		return ExecutionOutput{}, errors.New("test case script is empty")
	}
	if !e.AllowedCommands[fields[0]] {
		return ExecutionOutput{}, fmt.Errorf("command %q is not allowed", fields[0])
	}
	cmd := exec.CommandContext(ctx, fields[0], fields[1:]...)
	if e.WorkingDir != "" {
		cmd.Dir = e.WorkingDir
	}
	output, err := cmd.CombinedOutput()
	return ExecutionOutput{Actual: strings.TrimSpace(string(output)), Logs: string(output)}, err
}

// APIExecutor executes a test case as an HTTP request. The case Script may be
// an absolute URL or a path appended to BaseURL. A case passes when the HTTP
// status matches ExpectedStatus and ExpectedResult is empty or occurs in the
// response body.
type APIExecutor struct {
	Config ExecutorConfig
	Client *http.Client
}

func (e APIExecutor) Execute(ctx context.Context, tc TestCase) (ExecutionOutput, error) {
	cfg := e.Config
	method := strings.ToUpper(strings.TrimSpace(cfg.Method))
	if method == "" {
		method = http.MethodGet
	}
	if tc.Metadata != nil {
		if value, ok := tc.Metadata["method"].(string); ok && strings.TrimSpace(value) != "" {
			method = strings.ToUpper(strings.TrimSpace(value))
		}
	}
	if !allowedHTTPMethod(method) {
		return ExecutionOutput{}, fmt.Errorf("http method %q is not allowed", method)
	}
	base, baseErr := url.Parse(strings.TrimSpace(cfg.BaseURL))
	if baseErr != nil || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") {
		return ExecutionOutput{}, errors.New("api executor base_url is invalid")
	}
	target := strings.TrimSpace(tc.Script)
	if tc.Metadata != nil {
		if value, ok := tc.Metadata["url"].(string); ok && strings.TrimSpace(value) != "" {
			target = strings.TrimSpace(value)
		}
		if value, ok := tc.Metadata["path"].(string); ok && strings.TrimSpace(value) != "" {
			target = strings.TrimSpace(value)
		}
	}
	if target == "" {
		target = cfg.BaseURL
	}
	if target == "" {
		return ExecutionOutput{}, errors.New("api executor base_url is required")
	}
	u, err := url.Parse(target)
	if err != nil || u.Scheme == "" || u.Host == "" {
		target = strings.TrimRight(cfg.BaseURL, "/") + "/" + strings.TrimLeft(target, "/")
	}
	resolved, err := url.Parse(target)
	if err != nil || resolved.Host != base.Host || resolved.Scheme != base.Scheme {
		return ExecutionOutput{}, errors.New("test case URL must use the configured base_url origin")
	}
	var body io.Reader
	if tc.Metadata != nil {
		if v, ok := tc.Metadata["body"]; ok {
			if s, ok := v.(string); ok {
				body = strings.NewReader(s)
			} else if b, err := json.Marshal(v); err == nil {
				body = bytes.NewReader(b)
			}
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return ExecutionOutput{}, err
	}
	for k, v := range cfg.Headers {
		req.Header.Set(k, v)
	}
	if strings.TrimSpace(cfg.Token) != "" && req.Header.Get("Authorization") == "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(cfg.Token))
	}
	if body != nil && req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}
	client := e.Client
	if client == nil {
		client = &http.Client{}
	}
	resp, err := client.Do(req)
	if err != nil {
		return ExecutionOutput{}, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return ExecutionOutput{}, err
	}
	actual := strings.TrimSpace(string(b))
	expectedStatus := cfg.ExpectedStatus
	if tc.Metadata != nil {
		if value, ok := tc.Metadata["expected_status"]; ok {
			switch x := value.(type) {
			case int:
				expectedStatus = x
			case float64:
				expectedStatus = int(x)
			case string:
				if parsed, parseErr := strconv.Atoi(x); parseErr == nil {
					expectedStatus = parsed
				}
			}
		}
	}
	statusMatched := resp.StatusCode >= 200 && resp.StatusCode < 300
	if expectedStatus > 0 {
		statusMatched = resp.StatusCode == expectedStatus
	}
	passed := statusMatched && (strings.TrimSpace(tc.ExpectedResult) == "" || strings.Contains(actual, strings.TrimSpace(tc.ExpectedResult)))
	return ExecutionOutput{Actual: actual, Logs: fmt.Sprintf("HTTP %s %s -> %d\n%s", method, target, resp.StatusCode, actual), Passed: &passed, Data: map[string]interface{}{"status_code": resp.StatusCode}}, nil
}

// ValidateExecutorConfig rejects unsafe or incomplete runtime settings.
func ValidateExecutorConfig(cfg ExecutorConfig) error {
	typ := strings.ToLower(strings.TrimSpace(cfg.Type))
	if typ == "" {
		typ = "none"
	}
	if typ != "none" && typ != "api" && typ != "command" {
		return fmt.Errorf("unsupported executor type %q", cfg.Type)
	}
	if !cfg.Enabled || typ == "none" {
		return nil
	}
	if cfg.TimeoutMS < 0 {
		return errors.New("timeout_ms must be non-negative")
	}
	if typ == "api" {
		u, err := url.Parse(strings.TrimSpace(cfg.BaseURL))
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
			return errors.New("base_url must be an http(s) URL")
		}
		if cfg.ExpectedStatus != 0 && (cfg.ExpectedStatus < 100 || cfg.ExpectedStatus > 599) {
			return errors.New("expected_status must be between 100 and 599")
		}
		if method := strings.ToUpper(strings.TrimSpace(cfg.Method)); method != "" && !allowedHTTPMethod(method) {
			return fmt.Errorf("http method %q is not allowed", method)
		}
	}
	if typ == "command" && len(cfg.AllowedCommands) == 0 {
		return errors.New("allowed_commands is required for command executor")
	}
	return nil
}

func allowedHTTPMethod(method string) bool {
	switch method {
	case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodHead, http.MethodOptions:
		return true
	}
	return false
}

// NewExecutorFromConfig turns persisted settings into a safe executor.
func NewExecutorFromConfig(cfg ExecutorConfig) (TestCaseExecutor, error) {
	if err := ValidateExecutorConfig(cfg); err != nil {
		return nil, err
	}
	typ := strings.ToLower(strings.TrimSpace(cfg.Type))
	if typ == "" {
		typ = "none"
	}
	if !cfg.Enabled || typ == "none" {
		return nil, nil
	}
	if typ == "api" {
		hc := &http.Client{}
		if cfg.TimeoutMS > 0 {
			hc.Timeout = time.Duration(cfg.TimeoutMS) * time.Millisecond
		}
		return APIExecutor{Config: cfg, Client: hc}, nil
	}
	allowed := map[string]bool{}
	for _, command := range cfg.AllowedCommands {
		command = strings.TrimSpace(command)
		if command != "" {
			allowed[command] = true
		}
	}
	return ConfiguredCommandExecutor{CommandExecutor: CommandExecutor{AllowedCommands: allowed}, DefaultCommand: cfg.Command}, nil
}
