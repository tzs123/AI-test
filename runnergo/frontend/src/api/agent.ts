export interface AgentTask {
  id?: string
  name?: string
  description: string
  project_id?: number
  context?: Record<string, unknown>
}

export interface AgentStatus {
  task: AgentTask
  test_cases: Array<Record<string, unknown>>
  results: Array<Record<string, unknown>>
  quality: Record<string, unknown>
  steps: string[]
}

export async function runAgent(task: AgentTask): Promise<AgentStatus> {
  const response = await fetch('/api/v1/agent/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(task),
  })
  if (!response.ok) throw new Error(await response.text())
  return response.json()
}

export async function getAgentStatus(id: string): Promise<AgentStatus> {
  const response = await fetch(`/api/v1/agent/tasks/${encodeURIComponent(id)}`, { credentials: 'include' })
  if (!response.ok) throw new Error(await response.text())
  return response.json()
}
