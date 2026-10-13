// Shapes returned by the FastAPI backend (backend/src/agentfinx/api: app.py, imports.py, discovery.py).

export type Features = { report_discovery: boolean; chat_context: boolean };
export type User = { id: string; name: string; email: string };

export type Dataset = {
  id: string;
  label: string;
  company: string;
  ticker: string;
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  scope: string;
  audit_status: string;
  ready: boolean;
  access: "owned" | "shared";
};

export type Session = {
  id: string;
  title: string;
  dataset_id: string | null;
  dataset_label: string;
  read_only: boolean;
  read_only_reason: string;
  read_only_message: string;
  active_run_id: string | null;
  import_job_id: string | null;
  /** A run whose company news is still being finished (resume after reload). */
  news_update_run_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type MessageKind =
  | "text"
  | "report_search_request"
  | "report_candidates"
  | "report_search_clarification"
  | "report_search_error"
  | "dataset_required"
  | "context_clarification"
  | "context_error"
  | string;

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  run_id: string | null;
  created_at: string;
  kind: MessageKind;
  // Server-built JSON for structured kinds; rendered as text only.
  payload: any | null;
  context_status?: string;
  context_reason?: string;
  interpreted_question?: string;
};

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted";

export type Run = {
  run_id: string;
  session_id: string;
  status: RunStatus;
  error_code: string;
  error_message: string;
  answer: Message | null;
  task_kind: string;
  result_kind: string;
  override_of_run_id: string | null;
  context_status?: string;
  context_reason?: string;
  interpreted_question?: string;
  /** Company news finished after the answer, shown by itself when ready. */
  news_update?: { status: "pending" | "done" | "expired" | "failed"; message: Message | null } | null;
};

export type ImportJob = {
  job_id: string;
  session_id: string;
  origin: string;
  status: "queued" | "downloading" | "parsing" | "validating" | "building" | "ready" | "failed";
  billing_status: "not_submitted" | "pending" | "confirmed" | "estimated_legacy";
  billing_pending: boolean;
  billing_blocking: boolean;
  estimated_credits: number;
  confirmed_credits: string;
  page_count: number;
  allow_cloud_parse: boolean;
  dataset_id: string;
  session_attached: boolean;
  error_code: string;
  error_message: string;
  target_session_id?: string;
};

export type Candidate = {
  candidate_id: string;
  title: string;
  ticker: string;
  company: string;
  exchange: string;
  period_type: "annual" | "quarter" | "half_year" | "unknown";
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  fiscal_half: number | null;
  period_label: string;
  period_verified: boolean;
  verification_note: string;
  scope: string;
  audit_status: string;
  source_url: string;
  source_host: string;
  expires_at: string;
};

export type DiscoverySearch = {
  subject?: string;
  ticker?: string;
  fiscal_year?: number;
  period?: "any" | "annual" | "quarter" | "half_year";
  fiscal_quarter?: number;
  fiscal_half?: number;
  scope?: string;
  audit_status?: string;
};

export type DiscoveryResult = {
  session_id: string;
  request_message: Message;
  result_message: Message;
  replayed: boolean;
};

export type Health = {
  api: string;
  workers: Record<string, { status: string; seconds_since_heartbeat?: number }>;
};
