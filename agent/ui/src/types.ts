export interface StartResponse {
  workflow_id: string;
}

export interface ProgressResponse {
  workflow_id: string;
  status?: string;
  steps: string[];
  tool_calls: string[];
  answer: string | null;
  model: string | null;
  done: boolean;
  /** Keycard policy denials the workflow recorded (one per refused tool call). */
  denials?: string[];
}

export interface AccessState {
  application: string;
  resource: string;
  /** "policy": a forbid policy is toggled; "dependency": the app's dependency list is edited. */
  mechanism?: "policy" | "dependency";
  /** Name of the forbid policy when mechanism is "policy". */
  policy?: string;
  policy_set?: string | null;
  policy_set_version?: number | null;
  /** null when the zone did not report the state. */
  allowed: boolean | null;
}
