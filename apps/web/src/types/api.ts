/** 与 packages/schema 对齐的接口类型（手写，随后端契约同步）。 */

export interface ContributorItem {
  name: string
  value: number | null
  weight: number
  q: number
  contribution: number
}

export interface PredictionItem {
  variety_code: string
  target_date: string
  asof: string
  direction: number
  bucket: string
  p_up: number
  p_flat: number
  p_down: number
  exp_change: number | null
  signal: number
  confidence: number
  contributor: ContributorItem[]
  model_version: string
  narrative: string | null
}

export interface PredictionListResponse {
  predictions: PredictionItem[]
}

export interface TriggerDoc {
  id: number
  title: string
  url: string | null
  source: string
  published_at: string | null
}

export interface RevisionItem {
  id: number
  prediction_id: number
  variety_code: string
  target_date: string
  at: string
  prev_signal: number | null
  new_signal: number | null
  prev_direction: number | null
  new_direction: number | null
  reason: string
  is_surprise: boolean
  trigger_doc: TriggerDoc | null
}

export interface RevisionListResponse {
  revisions: RevisionItem[]
  alerts: string[]
}

export interface VarietyInfo {
  code: string
  name: string
  sector: string
}

export interface SignalPoint {
  target_date: string
  signal: number
  direction: number
  bucket: string
  confidence: number
}

export interface PricePoint {
  date: string
  close: number
}

export interface NewsRef {
  doc_id: number
  title: string
  url: string | null
  source: string
  published_at: string | null
  direction: number
  strength: number
  chain_stage: string | null
  is_surprise: boolean
  evidence: string | null
}

export interface ModelInfo {
  version: string
  kind: string
  status: string
  calibration: Record<string, number | string | null>
}

export interface VarietyDetail {
  variety: VarietyInfo
  prediction: PredictionItem | null
  signal_history: SignalPoint[]
  prices: PricePoint[]
  bullish: NewsRef[]
  bearish: NewsRef[]
  revisions: RevisionItem[]
  model: ModelInfo | null
}

export interface ScorecardDayItem {
  variety_code: string
  name: string
  bucket: string
  signal: number
  actual_change: number
  actual_bucket: string
  hit: boolean
}

export interface ScorecardDay {
  date: string
  n: number
  direction_accuracy: number | null
  items: ScorecardDayItem[]
}

export interface ScorecardVariety {
  variety_code: string
  name: string
  n: number
  coverage: number
  direction_accuracy: number | null
}

export interface ScorecardResponse {
  window_days: number
  model_version: string | null
  scored_through: string | null
  overall: {
    n: number
    coverage: number
    direction_n: number
    direction_accuracy: number | null
    brier: number | null
  }
  by_day: ScorecardDay[]
  by_variety: ScorecardVariety[]
  latest: ScorecardDay | null
}

export interface ExtractItem {
  id: number
  variety_code: string
  variety_name: string
  direction: number
  strength: number
  chain_stage: string | null
  is_surprise: boolean
  method: string
  evidence: string | null
  doc: TriggerDoc | null
}

export interface ExtractListResponse {
  extracts: ExtractItem[]
}

export interface VarietyMeta {
  code: string
  name: string
  sector: string
}

export interface VarietyListResponse {
  varieties: VarietyMeta[]
}
