package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/runnergo/runnergo/backend/internal/agent"
	"github.com/runnergo/runnergo/backend/internal/handler"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
)

func main() {
	gin.SetMode(gin.ReleaseMode)
	kb := agent.NewKnowledgeBase()
	queue := agent.TaskQueue(agent.NewMemoryTaskQueue(100))
	if addr := os.Getenv("RUNNERGO_REDIS_ADDR"); addr != "" {
		client := redis.NewClient(&redis.Options{Addr: addr, Password: os.Getenv("RUNNERGO_REDIS_PASSWORD"), DB: 0})
		pingCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		if err := client.Ping(pingCtx).Err(); err == nil {
			durable := agent.NewDurableRedisTaskQueue(client, os.Getenv("RUNNERGO_AGENT_QUEUE_PREFIX"))
			if recoverErr := durable.Recover(context.Background()); recoverErr != nil {
				log.Printf("redis queue recovery failed: %v", recoverErr)
			}
			queue = durable
		} else {
			log.Printf("redis queue unavailable, using in-memory queue: %v", err)
		}
		cancel()
	}
	engine := agent.NewEngineFromEnv(kb, queue, nil)
	var configStore agent.ExecutorConfigStore = agent.NewMemoryExecutorConfigStore()
	var taskStore agent.AgentTaskStore
	if dsn := os.Getenv("RUNNERGO_MYSQL_DSN"); dsn != "" {
		if db, err := migrateWithRetry(dsn); err != nil {
			log.Printf("agent database migration skipped: %v", err)
		} else {
			configStore = agent.GORMExecutorConfigStore{DB: db}
			taskStore = agent.GORMAgentTaskStore{DB: db}
			if persisted, loadErr := taskStore.LoadKnowledge(context.Background(), 0); loadErr == nil {
				for _, item := range persisted {
					kb.Add(item)
				}
			}
			if cfg, loadErr := configStore.Load(context.Background(), 0); loadErr == nil && cfg.ID != 0 {
				_ = engine.ApplyExecutorConfig(cfg)
			}
			if loader, ok := configStore.(interface {
				LoadAll(context.Context) ([]agent.ExecutorConfig, error)
			}); ok {
				if configs, loadErr := loader.LoadAll(context.Background()); loadErr == nil {
					for _, cfg := range configs {
						_ = engine.ApplyExecutorConfig(cfg)
					}
				}
			}
		}
	}
	router := gin.Default()
	_ = router.SetTrustedProxies([]string{"127.0.0.1", "172.16.0.0/12"})
	router.GET("/health", func(c *gin.Context) { c.JSON(200, gin.H{"status": "ok"}) })
	authRequired := strings.ToLower(os.Getenv("RUNNERGO_AGENT_AUTH_REQUIRED")) != "false"
	remoteAuth := NewRemoteAuthorizer(os.Getenv("RUNNERGO_AGENT_AUTH_VERIFY_URL"))
	authFunc := func(c *gin.Context) bool {
		if !authRequired {
			return true
		}
		if remoteAuth.URL != "" {
			return remoteAuth.Authorize(c)
		}
		return c.GetHeader("Authorization") != ""
	}
	h := handler.NewAgentHandlerWithStores(engine, authFunc, configStore, taskStore)
	h.SetProjectAuthorizer(func(c *gin.Context, projectID uint) bool {
		if remoteAuth.URL == "" || projectID == 0 {
			return true
		}
		return remoteAuth.AuthorizeProject(c, projectID)
	})
	h.RegisterRoutes(router.Group("/api/v1"))
	engine.OnTaskStart = h.MarkRunning
	go engine.RunWorker(context.Background(), func(result agent.OrchestrationResult, runErr error) {
		h.RecordResult(result, runErr)
	})
	port := os.Getenv("RUNNERGO_AGENT_PORT")
	if port == "" {
		port = "8088"
	}
	_ = router.Run(":" + port)
}

// RemoteAuthorizer delegates identity and project membership checks to the
// existing Django TestHub authentication stack, keeping JWT secrets out of
// the Go service and preserving RunnerGo's current permission semantics.
type RemoteAuthorizer struct {
	URL    string
	Client *http.Client
}

func NewRemoteAuthorizer(url string) *RemoteAuthorizer {
	return &RemoteAuthorizer{URL: strings.TrimRight(strings.TrimSpace(url), "/"), Client: &http.Client{Timeout: 5 * time.Second}}
}
func (a *RemoteAuthorizer) Authorize(c *gin.Context) bool {
	if a == nil || a.URL == "" {
		return c.GetHeader("Authorization") != ""
	}
	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodGet, a.URL, nil)
	if err != nil {
		return false
	}
	if token := c.GetHeader("Authorization"); token != "" {
		req.Header.Set("Authorization", token)
	} else {
		return false
	}
	resp, err := a.Client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false
	}
	var payload struct {
		ID uint `json:"id"`
	}
	if body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)); readErr == nil {
		_ = json.Unmarshal(body, &payload)
	}
	if payload.ID != 0 {
		c.Set("user_id", payload.ID)
	}
	return true
}
func (a *RemoteAuthorizer) AuthorizeProject(c *gin.Context, projectID uint) bool {
	if a == nil || a.URL == "" || projectID == 0 {
		return true
	}
	base := strings.TrimSuffix(a.URL, "/api/auth/me")
	if strings.HasSuffix(base, "/api/users/me") {
		base = strings.TrimSuffix(base, "/api/users/me")
	}
	url := base + "/api/projects/" + fmt.Sprint(projectID) + "/"
	req, err := http.NewRequestWithContext(c.Request.Context(), http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	if token := c.GetHeader("Authorization"); token != "" {
		req.Header.Set("Authorization", token)
	} else {
		return false
	}
	resp, err := a.Client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

func migrateWithRetry(dsn string) (*gorm.DB, error) {
	var last error
	for attempt := 0; attempt < 10; attempt++ {
		db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{})
		if err == nil {
			if err = agent.Migrate(db); err == nil {
				if err = agent.MigrateExecutorConfig(db); err == nil {
					return db, nil
				}
			}
		}
		last = err
		time.Sleep(time.Duration(attempt+1) * time.Second)
	}
	return nil, last
}
