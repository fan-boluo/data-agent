// 评测接口返回的是数据库原始列（snake_case），此处按实际结构声明

export interface EvalDataset {
  id: number
  name: string
  description?: string
  created_at?: string
  case_total: number
  case_verified: number
}

export interface EvalRunMetrics {
  ex?: number | null
  ves?: number | null
  valid_sql?: number | null
  tool_success_rate?: number | null
  avg_steps?: number | null
  avg_duration_ms?: number | null
  context_usage_avg?: number | null
  context_compress_ratio?: number | null
  case_scored?: number
  categories?: Record<string, number>
}

export interface EvalRun {
  id: number
  dataset_id: number
  name: string
  status: string
  source: string
  baseline_run_id?: number | null
  limit_count: number
  case_total: number
  case_done: number
  case_failed: number
  metrics?: EvalRunMetrics
  report_md?: string
  error?: string
  started_at?: string
  ended_at?: string
  created_at?: string
  updated_at?: string
}
