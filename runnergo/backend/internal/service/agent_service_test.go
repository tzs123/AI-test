package service

import (
	"context"
	"testing"

	"github.com/runnergo/runnergo/backend/internal/agent"
)

func TestAgentServiceAuthAndMethods(t *testing.T) {
	s := NewAgentService(nil)
	if _, err := s.Generate(context.Background(), 0, agent.TestTask{Description: "x"}); err == nil {
		t.Fatal("auth")
	}
	cases, err := s.Generate(context.Background(), 1, agent.TestTask{Description: "x"})
	if err != nil || len(cases) != 3 {
		t.Fatal(len(cases), err)
	}
	results, err := s.Execute(context.Background(), 1, cases)
	if err != nil || len(results) != 3 {
		t.Fatal(len(results), err)
	}
	run, err := s.Run(context.Background(), 1, agent.TestTask{Description: "x"})
	if err != nil || run.Quality.TotalCases != 3 {
		t.Fatal(run, err)
	}
	if _, err := s.Execute(context.Background(), 0, cases); err == nil {
		t.Fatal("auth")
	}
	if _, err := s.Run(context.Background(), 0, agent.TestTask{Description: "x"}); err == nil {
		t.Fatal("auth")
	}
}
