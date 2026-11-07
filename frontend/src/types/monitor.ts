export interface MonitorPageData<T> {
  records: T[]
  pageNumber: number
  pageSize: number
  totalPage: number
  totalRow: number
  optimizeCountQuery?: boolean
}

export interface MonitorToolCallDetail {
  name: string
  latencyMs: number
  status: string
  callCount: number
}

export interface MonitorSessionSummary {
  sessionId: string
  traceId: string
  appId: string
  userId: string
  model: string
  status: string
  totalTurns: number
  totalPromptTokens: number
  totalCompletionTokens: number
  tokenBudget: number
  avgLlmLatencyMs: number
  avgFirstTokenMs: number
  maxLlmLatencyMs: number
  minLlmLatencyMs: number
  totalToolCalls: number
  totalErrors: number
  recoveryCount: number
  lastRecoveryKind: string
  endReason: string
  startedAt?: string
  endedAt?: string
  durationMs: number
  updatedAt?: string
}

export interface MonitorTurnSummary {
  traceId: string
  sessionId: string
  turnId: string
  turnNumber: number
  status: string
  promptTokens: number
  completionTokens: number
  llmLatencyMs: number
  firstTokenMs: number
  llmRecoveryCount: number
  llmRecoveryKind: string
  toolCallsCount: number
  toolCallsDetail: MonitorToolCallDetail[]
  memoryRetrievalMs: number
  contextTokens: number
  contextTokenUsage: number
  errorCount: number
  startedAt?: string
  endedAt?: string
  durationMs: number
  createdAt?: string
}

export interface SessionPoolStats {
  totalSessions: number
  runningSessions: number
  idleSessions: number
  closedSessions: number
  maxSessions: number
}

export interface PoolSessionSummary {
  sessionId: string
  appId: string
  userId: string
  agentName: string
  status: string
  activeTasks: number
  pendingRequests: number
  messageCount: number
  lastActivityAt?: string
  idleSeconds: number
}

export interface MergedSessionRow {
  sessionId: string
  appId: string
  userId: string
  status: string
  inPool: boolean
  totalTurns: number
  messageCount: number
  lastActivityAt?: string
  idleSeconds: number
  pendingRequests: number
}

export interface MonitorSpanNode {
  spanId: string
  parentSpanId: string
  opType: string
  name: string
  agent: string
  seq: number
  status: string
  error?: string | null
  startedAt?: string
  durationMs: number
  offsetMs: number
  attributes: Record<string, unknown>
}

export interface MonitorTurnSpanTree {
  traceId: string
  sessionId: string
  turnId: string
  startedAt?: string
  totalDurationMs: number
  spanCount: number
  spans: MonitorSpanNode[]
}

export interface MonitorAlertRecordVO {
  id?: number
  ruleName: string
  level: string
  traceId: string
  sessionId: string
  turnId: string
  status: string
  message: string
  observedValue: string
  thresholdValue: string
  triggeredAt?: string
  resolvedAt?: string
  payload: Record<string, unknown>
}

export interface MonitorOverviewStats {
  totalSessions: number
  runningSessions: number
  successSessions: number
  errorSessions: number
  totalTurns: number
  avgTurnDurationMs: number
  avgLlmLatencyMs: number
  avgFirstTokenMs: number
  avgContextTokens: number
  avgContextTokenUsage: number
  totalToolCalls: number
  totalTokens: number
  openAlerts: number
}

export interface MonitorSessionDetail {
  session: MonitorSessionSummary
  turns: MonitorTurnSummary[]
  alerts: MonitorAlertRecordVO[]
}

export interface MonitorCleanupTableResult {
  tableName: string
  status: string
  affectedRows: number
  cutoffAt?: string
  errorMessage: string
}

export interface MonitorCleanupSummary {
  retentionDays: number
  dryRun: boolean
  status: string
  deletedRows: number
  executedAt?: string
  tableResults: MonitorCleanupTableResult[]
}

export interface MonitorStorageConfig {
  enabled: boolean
  persistSpans: boolean
  persistSessionMetrics: boolean
  persistTurnMetrics: boolean
  useRedisWindows: boolean
}

export interface MonitorThresholdRuleConfig {
  enabled: boolean
  level: string
  thresholdSeconds?: number
  thresholdRatio?: number
  thresholdCount?: number
  thresholdTurns?: number
  windowSize?: number
}

export interface MonitorConfigResponse {
  enabled: boolean
  storage: MonitorStorageConfig
  alerts: {
    llmAvgLatencyLast5: MonitorThresholdRuleConfig
    llmSingleTimeout: MonitorThresholdRuleConfig
    tokenQuotaUsage: MonitorThresholdRuleConfig
    toolConsecutiveFailures: MonitorThresholdRuleConfig
    sessionMaxTurns: MonitorThresholdRuleConfig
  }
}
