package agent

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"time"
)

var ErrQueueClosed = errors.New("task queue is closed")

// MemoryTaskQueue is a bounded, concurrency-safe queue suitable for a single
// service instance. Redis-backed implementations can satisfy TaskQueue.
type MemoryTaskQueue struct {
	ch     chan TestTask
	once   sync.Once
	closed chan struct{}
}

func NewMemoryTaskQueue(size int) *MemoryTaskQueue {
	if size < 1 {
		size = 1
	}
	return &MemoryTaskQueue{ch: make(chan TestTask, size), closed: make(chan struct{})}
}
func (q *MemoryTaskQueue) Enqueue(task TestTask) error {
	select {
	case <-q.closed:
		return ErrQueueClosed
	default:
	}
	select {
	case <-q.closed:
		return ErrQueueClosed
	case q.ch <- task:
		return nil
	default:
		return errors.New("task queue is full")
	}
}
func (q *MemoryTaskQueue) Dequeue(ctx context.Context) (TestTask, error) {
	select {
	case <-q.closed:
		return TestTask{}, ErrQueueClosed
	default:
	}
	select {
	case <-ctx.Done():
		return TestTask{}, ctx.Err()
	case <-q.closed:
		return TestTask{}, ErrQueueClosed
	case task := <-q.ch:
		return task, nil
	}
}
func (q *MemoryTaskQueue) Len() int { return len(q.ch) }
func (q *MemoryTaskQueue) Close()   { q.once.Do(func() { close(q.closed) }) }

type MemoryResultCollector struct {
	mu      sync.RWMutex
	results []TestResult
}

func NewMemoryResultCollector() *MemoryResultCollector { return &MemoryResultCollector{} }
func (c *MemoryResultCollector) Collect(result TestResult) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.results = append(c.results, result)
}
func (c *MemoryResultCollector) Results() []TestResult {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]TestResult, len(c.results))
	copy(out, c.results)
	return out
}
func (c *MemoryResultCollector) Reset() { c.mu.Lock(); defer c.mu.Unlock(); c.results = nil }

// RedisClient is a small adapter contract. A go-redis client can be wrapped
// with two closures, avoiding a mandatory Redis SDK dependency here.
type RedisClient interface {
	LPush(context.Context, string, ...interface{}) (int64, error)
	BRPop(context.Context, time.Duration, ...string) ([]string, error)
}
type RedisLengthClient interface {
	LLen(context.Context, string) (int64, error)
}
type RedisTaskQueue struct {
	Client RedisClient
	Key    string
}

func (q RedisTaskQueue) key() string {
	if q.Key == "" {
		return "runnergo:agent:tasks"
	}
	return q.Key
}
func (q RedisTaskQueue) Enqueue(task TestTask) error {
	if q.Client == nil {
		return errors.New("redis client is nil")
	}
	b, err := json.Marshal(task)
	if err != nil {
		return err
	}
	_, err = q.Client.LPush(context.Background(), q.key(), string(b))
	return err
}
func (q RedisTaskQueue) Dequeue(ctx context.Context) (TestTask, error) {
	if q.Client == nil {
		return TestTask{}, errors.New("redis client is nil")
	}
	v, err := q.Client.BRPop(ctx, 0, q.key())
	if err != nil {
		return TestTask{}, err
	}
	if len(v) < 2 {
		return TestTask{}, errors.New("invalid redis task")
	}
	var t TestTask
	if err := json.Unmarshal([]byte(v[1]), &t); err != nil {
		return TestTask{}, err
	}
	return t, nil
}
func (q RedisTaskQueue) Len() int {
	if c, ok := q.Client.(RedisLengthClient); ok {
		n, err := c.LLen(context.Background(), q.key())
		if err == nil && n > 0 {
			return int(n)
		}
	}
	return 0
}
