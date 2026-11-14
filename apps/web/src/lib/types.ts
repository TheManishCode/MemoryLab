/** Shapes of the API's JSON (services/api). Only what the screens read. */
export type RunStatus = "queued" | "starting" | "running" | "session_boundary" | "dreaming" | "stopping" | "completed" | "capped" | "failed" | "killed";
export const FINAL_STATES: readonly RunStatus[] = ["completed", "capped", "failed", "killed"];
export type Visibility = "private" | "unlisted" | "public";

export interface Run {
  id: string;
  owner_id: string;
  title: string | null;
  status: RunStatus;
  visibility: Visibility;
  game: string | null;
  parent_checkpoint_id: string | null;
  fork_patch: Record<string, unknown> | null;
  model_id: string | null;
  provider: string | null;
  caps: { max_steps: number; max_tokens: number; max_wall_s: number };
  steps: number;
  tokens: number;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  created_at: string;
}

export interface RunEvent {
  seq: number;
  run_id: string;
  step: number;
  ts: string;
  type: string;
  data: Record<string, unknown>;
}

export interface Me {
  id: string;
  handle: string | null;
  role: "user" | "admin";
  created_at: string;
  active_runs: number;
  limits: { concurrent_runs: number; runs_per_hour: number; reads_per_minute: number };
}

export interface Game { id: string; name: string; licence: string; state_map_version: string }
export interface Preset { id: string; version: number; ref: string; text: string; text_sha256: string; label: string; is_adaptation: boolean }
export interface Models { providers: Record<string, string[]>; price_table: { note: string } }
export interface MemoryNode { path: string; chars: number; version: number }
export interface Checkpoint { id: string; step: number; memory_version: number; config_hash: string }
export interface LeaderRow { id: string; model_id: string | null; provider: string | null; progress_score: number; steps: number; tokens_in: number; tokens_out: number }
export interface ExperimentRun { run_id: string; status: RunStatus; title: string | null; model_id: string | null; provider: string | null; steps: number; tokens: number; progress_score: number | null; verified: boolean }
export interface ExperimentArm {
  label: string; value: unknown; n: number; mean: number | null; median: number | null;
  stdev: number | null; min: number | null; max: number | null; ci95: [number, number] | null;
}
export interface ExperimentComparison {
  a_label: string; b_label: string; diff_means: number; diff_ci95: [number, number];
  p_value: number; exact: boolean; significant: boolean; underpowered: boolean; note: string;
}
export interface Experiment {
  id: string; owner_id: string; title: string; variable: string; visibility: Visibility;
  runs: ExperimentRun[];
  summary: {
    n: number; mean: number | null; median: number | null; min: number | null; max: number | null;
    stdev: number | null; ci95: [number, number] | null; runs: number; runs_in_progress: number;
    warnings: string[]; arms: ExperimentArm[]; comparison: ExperimentComparison | null;
  };
}
export interface Compare { metric: string; steps: number[]; series: { run_id: string; values: (number | null)[] }[] }
export interface AdminReport { id: string; run_id: string; reporter_id: string; reason: string; status: string; created_at: string; run_title: string | null; run_visibility: string; run_status: string; run_deleted: boolean }
