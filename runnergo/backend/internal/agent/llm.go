package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

var ErrLLMUnavailable = errors.New("llm client is not configured")

// callLLM supports common provider client shapes without coupling the engine
// to one SDK. The context is always propagated so callers can enforce a hard
// timeout.
func callLLM(ctx context.Context, client LLMClient, prompt string) (string, error) {
	if client == nil {
		return "", ErrLLMUnavailable
	}
	switch c := client.(type) {
	case interface {
		Generate(context.Context, string) (string, error)
	}:
		return c.Generate(ctx, prompt)
	case interface {
		Complete(context.Context, string) (string, error)
	}:
		return c.Complete(ctx, prompt)
	case interface {
		Chat(context.Context, string) (string, error)
	}:
		return c.Chat(ctx, prompt)
	case interface{ Generate(string) (string, error) }:
		return callWithoutContext(ctx, func() (string, error) { return c.Generate(prompt) })
	case interface{ Complete(string) (string, error) }:
		return callWithoutContext(ctx, func() (string, error) { return c.Complete(prompt) })
	default:
		return "", fmt.Errorf("unsupported llm client %T", client)
	}
}

func callWithoutContext(ctx context.Context, fn func() (string, error)) (string, error) {
	result := make(chan struct {
		out string
		err error
	}, 1)
	go func() {
		out, err := fn()
		result <- struct {
			out string
			err error
		}{out, err}
	}()
	select {
	case v := <-result:
		return v.out, v.err
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// RetryLLM wraps a client with bounded retries and exponential backoff.
type RetryLLM struct {
	Client  LLMClient
	Retries int
	Backoff time.Duration
}

func (r RetryLLM) Generate(ctx context.Context, prompt string) (string, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	retries := r.Retries
	if retries < 0 {
		retries = 0
	}
	backoff := r.Backoff
	if backoff <= 0 {
		backoff = 100 * time.Millisecond
	}
	var last error
	for attempt := 0; attempt <= retries; attempt++ {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		out, err := callLLM(ctx, r.Client, prompt)
		if err == nil {
			return out, nil
		}
		last = err
		if attempt == retries {
			break
		}
		t := time.NewTimer(backoff * time.Duration(1<<attempt))
		select {
		case <-ctx.Done():
			t.Stop()
			return "", ctx.Err()
		case <-t.C:
		}
	}
	return "", last
}

// OpenAIClient is a small JSON HTTP adapter compatible with the chat
// completions endpoint. It can also be pointed at OpenAI-compatible local
// servers by changing BaseURL.
type OpenAIClient struct {
	APIKey  string
	Model   string
	BaseURL string
	HTTP    *http.Client
	Retries int
	Timeout time.Duration
}

func (c *OpenAIClient) Generate(ctx context.Context, prompt string) (string, error) {
	if strings.TrimSpace(c.APIKey) == "" {
		return "", ErrLLMUnavailable
	}
	base := strings.TrimRight(c.BaseURL, "/")
	if base == "" {
		base = "https://api.openai.com/v1"
	}
	payload := map[string]interface{}{"model": c.Model, "messages": []map[string]string{{"role": "user", "content": prompt}}}
	body, _ := json.Marshal(payload)
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{}
	}
	if c.Timeout > 0 {
		copyClient := *hc
		copyClient.Timeout = c.Timeout
		hc = &copyClient
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/chat/completions", strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+c.APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := hc.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("llm http status %d", resp.StatusCode)
	}
	var decoded struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return "", err
	}
	if len(decoded.Choices) == 0 {
		return "", errors.New("llm returned no choices")
	}
	return decoded.Choices[0].Message.Content, nil
}

// LocalModelClient targets Ollama's /api/generate API.
type LocalModelClient struct {
	BaseURL, Model string
	HTTP           *http.Client
	Timeout        time.Duration
}

func (c *LocalModelClient) Generate(ctx context.Context, prompt string) (string, error) {
	base := strings.TrimRight(c.BaseURL, "/")
	if base == "" {
		base = "http://localhost:11434"
	}
	body, _ := json.Marshal(map[string]interface{}{"model": c.Model, "prompt": prompt, "stream": false})
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{}
	}
	if c.Timeout > 0 {
		cp := *hc
		cp.Timeout = c.Timeout
		hc = &cp
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/api/generate", strings.NewReader(string(body)))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := hc.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("local llm http status %d", resp.StatusCode)
	}
	var decoded struct {
		Response string `json:"response"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return "", err
	}
	return decoded.Response, nil
}
