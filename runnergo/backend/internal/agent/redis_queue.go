package agent

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"
)

// DurableTaskQueue extends TaskQueue with acknowledgement and recovery.
// Workers atomically move tasks to a processing list before execution.
type DurableTaskQueue interface {
	TaskQueue
	Ack(ctx context.Context, task TestTask) error
	Requeue(ctx context.Context, task TestTask) error
	Recover(ctx context.Context) error
}

type DurableRedisTaskQueue struct {
	Client        redis.UniversalClient
	QueueKey      string
	ProcessingKey string
	PollTimeout   time.Duration
}

func NewDurableRedisTaskQueue(client redis.UniversalClient, prefix string) *DurableRedisTaskQueue {
	if prefix == "" {
		prefix = "runnergo:agent"
	}
	return &DurableRedisTaskQueue{
		Client: client, QueueKey: prefix + ":queue", ProcessingKey: prefix + ":processing",
		PollTimeout: 5 * time.Second,
	}
}

func (q *DurableRedisTaskQueue) Enqueue(task TestTask) error {
	if q == nil || q.Client == nil {
		return errors.New("redis task queue is not configured")
	}
	b, err := json.Marshal(task)
	if err != nil {
		return err
	}
	return q.Client.LPush(context.Background(), q.QueueKey, b).Err()
}

func (q *DurableRedisTaskQueue) Dequeue(ctx context.Context) (TestTask, error) {
	if q == nil || q.Client == nil {
		return TestTask{}, errors.New("redis task queue is not configured")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	poll := q.PollTimeout
	if poll <= 0 {
		poll = 5 * time.Second
	}
	for {
		raw, err := q.Client.BRPopLPush(ctx, q.QueueKey, q.ProcessingKey, poll).Result()
		if err == nil {
			var task TestTask
			if decodeErr := json.Unmarshal([]byte(raw), &task); decodeErr != nil {
				_ = q.Client.LRem(ctx, q.ProcessingKey, 1, raw).Err()
				return TestTask{}, decodeErr
			}
			return task, nil
		}
		if errors.Is(err, redis.Nil) {
			if ctx.Err() != nil {
				return TestTask{}, ctx.Err()
			}
			continue
		}
		return TestTask{}, err
	}
}

func (q *DurableRedisTaskQueue) Ack(ctx context.Context, task TestTask) error {
	return q.remove(ctx, task)
}

func (q *DurableRedisTaskQueue) Requeue(ctx context.Context, task TestTask) error {
	if err := q.remove(ctx, task); err != nil {
		return err
	}
	b, err := json.Marshal(task)
	if err != nil {
		return err
	}
	return q.Client.LPush(ctx, q.QueueKey, b).Err()
}

func (q *DurableRedisTaskQueue) remove(ctx context.Context, task TestTask) error {
	if q == nil || q.Client == nil {
		return errors.New("redis task queue is not configured")
	}
	b, err := json.Marshal(task)
	if err != nil {
		return err
	}
	return q.Client.LRem(ctx, q.ProcessingKey, 1, b).Err()
}

func (q *DurableRedisTaskQueue) Recover(ctx context.Context) error {
	if q == nil || q.Client == nil {
		return errors.New("redis task queue is not configured")
	}
	for {
		moved, err := q.Client.RPopLPush(ctx, q.ProcessingKey, q.QueueKey).Result()
		if errors.Is(err, redis.Nil) {
			return nil
		}
		if err != nil {
			return err
		}
		if moved == "" {
			return nil
		}
	}
}

func (q *DurableRedisTaskQueue) Len() int {
	if q == nil || q.Client == nil {
		return 0
	}
	n, err := q.Client.LLen(context.Background(), q.QueueKey).Result()
	if err != nil {
		return 0
	}
	return int(n)
}
