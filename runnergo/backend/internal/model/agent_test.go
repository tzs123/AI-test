package model

import "testing"

func TestAgentModelAliases(t *testing.T) {
	task := AgentTask{Status: "created"}
	doc := KnowledgeDocument{Type: "document"}
	if task.Status != "created" || doc.Type != "document" || task.TableName() != "agent_tasks" || doc.TableName() != "knowledge_documents" {
		t.Fatal("model aliases unavailable")
	}
}
