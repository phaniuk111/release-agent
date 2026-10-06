/**
 * Wording and payloads for the Support tab — a 1:1 port of the portal's
 * static/core/support_triage.js and static/core/support_feedback.js (tested
 * there in tests/js/support_triage.test.mjs and support_feedback.test.mjs).
 * The server decides (tools/support/: what failed, the category, the action,
 * the owner, the ticket note, what feedback is stored and who gave it); this
 * only says it. Every function takes a payload with fields missing and still
 * answers. Keep the sentences identical to the portal's.
 */

export type SupportIncident = {
  id?: string;
  kind?: string;
  priority?: string;
  priority_reason?: string;
  title?: string;
  category?: string;
  count?: number;
  facts?: string[];
  error_text?: string;
  job_ids?: string[];
  runbook?: string;
  steps?: string[];
  owner?: string;
  action?: string;
  note?: string;
  shared?: Record<string, unknown>;
};

export type SupportReport = {
  ok?: boolean;
  disabled?: boolean;
  error?: string;
  hint?: string;
  empty?: boolean;
  business_date?: string;
  date_label?: string;
  previous_date?: string;
  scanned_at?: string;
  runbook_entries?: number;
  counts?: {
    items?: number;
    failed?: number;
    stuck?: number;
    missing?: number;
    recovered?: number;
  };
  labels?: Record<string, string>;
  job_url?: string;
  incidents?: SupportIncident[];
  notes?: string[];
};

/** The SSE "investigation" event's data: an Investigate answer is complete. */
export type InvestigationEvent = {
  business_date?: string;
  incident_id?: string;
  title?: string;
  category?: string;
  action?: string;
  model?: string;
  model_calls?: number | null;
  seconds?: number | null;
  shared?: boolean;
};

export type Verdict = 'right' | 'direction' | 'wrong';

export type FeedbackBody = {
  business_date: string;
  incident_id: string;
  title: string;
  verdict: string;
  actual_cause: string;
  category: string;
  action: string;
  model: string;
  model_calls?: number;
  seconds?: number;
};

export type FeedbackStats = {
  ok?: boolean;
  disabled?: boolean;
  days?: number;
  total?: number;
  right?: number;
  direction?: number;
  wrong?: number;
  accuracy?: number | null;
  right_or_direction?: number | null;
};

function num(v: unknown): number {
  const n = Number(v);
  return v === null || v === undefined || v === '' || !Number.isFinite(n) ? 0 : n;
}

function text(v: unknown): string {
  return v === null || v === undefined ? '' : String(v);
}

function obj<T extends object>(v: unknown): Partial<T> {
  return v && typeof v === 'object' ? (v as Partial<T>) : {};
}

// ---- triage -----------------------------------------------------------------

/** What each L1 action is called on the card. */
export const ACTION_LABELS: Record<string, string> = {
  wait: 'Wait for data',
  retrigger: 'Re-trigger once',
  check: 'Check',
  escalate: 'Escalate',
};

export function actionLabel(action: unknown): string {
  return ACTION_LABELS[String(action)] || 'Check';
}

/** "COB 2026-10-02 · 42 failed · 3 stuck · 5 missing of 610 runs", or "… all clear". */
export function summaryLine(report: unknown): string {
  const r = obj<SupportReport>(report);
  const label = String(r.date_label || 'Business date');
  if (r.disabled) return 'Support triage is disabled';
  if (r.ok === false) return 'Support triage is unavailable';
  if (!r.business_date) return 'no runs in the dates read';
  const c = obj<NonNullable<SupportReport['counts']>>(r.counts);
  const head = `${label} ${r.business_date}`;
  const runs = num(c.items) + (num(c.items) === 1 ? ' run' : ' runs');
  if (!num(c.failed) && !num(c.stuck) && !num(c.missing)) {
    return `${head} · all clear · ${runs}`;
  }
  const parts: string[] = [];
  if (num(c.failed)) parts.push(`${num(c.failed)} failed`);
  if (num(c.stuck)) parts.push(`${num(c.stuck)} stuck`);
  if (num(c.missing)) parts.push(`${num(c.missing)} missing`);
  return `${head} · ${parts.join(' · ')} of ${runs}`;
}

/** A line under the summary: retries that recovered and what was read. */
export function metaLine(report: unknown): string {
  const r = obj<SupportReport>(report);
  const parts: string[] = [];
  const c = obj<NonNullable<SupportReport['counts']>>(r.counts);
  if (num(c.recovered)) parts.push(`${num(c.recovered)} recovered after a retry`);
  if (r.previous_date) parts.push(`compared with ${r.previous_date}`);
  if (num(r.runbook_entries)) parts.push(`${num(r.runbook_entries)} runbook entries`);
  else if (r.ok !== false && !r.disabled) parts.push('no runbook configured — built-in actions');
  if (r.scanned_at) parts.push(`read ${r.scanned_at}`);
  return parts.join(' · ');
}

/** A job id as a link when SUPPORT_JOB_URL is set ("{job_id}" replaced), else "". */
export function jobHref(template: unknown, jobId: unknown): string {
  const t = String(template || '');
  const id = String(jobId || '');
  if (!t || !id || t.indexOf('{job_id}') === -1) return '';
  if (!(t.startsWith('https://') || t.startsWith('http://'))) return '';
  return t.split('{job_id}').join(encodeURIComponent(id));
}

/** What the chat is asked from an incident's "Ask why" — the model reads the
 *  same report through the support_triage tool; this names the incident. */
export function askPrompt(incident: unknown, report: unknown): string {
  const inc = obj<SupportIncident>(incident);
  const r = obj<SupportReport>(report);
  const date = r.business_date ? ` for ${r.business_date}` : '';
  return (
    `Support triage${date}: explain incident "${String(inc.title || '?')}` +
    '" — why it most likely happened, how urgent it is by our priority policy, ' +
    'what L1 should do now, and the ticket note.'
  );
}

/** What "Investigate" on an incident sends to the chat: the incident, its date,
 *  the values its runs share (the source above all), and its job ids — enough
 *  for the support-investigate skill to start without asking. */
export function investigatePrompt(incident: unknown, report: unknown): string {
  const inc = obj<SupportIncident>(incident);
  const r = obj<SupportReport>(report);
  // "Investigate incident <id> · <label> <date> · <title> …" — the id and the
  // date are what the investigate Workflow routes on (agent/parsing.py); the
  // rest is for a person reading the thread.
  const parts = [`Investigate incident ${String(inc.id || '?')}`];
  if (r.business_date) parts.push(`${r.date_label || 'business date'} ${r.business_date}`);
  parts.push(String(inc.title || '?'));
  const shared: Record<string, unknown> =
    inc.shared && typeof inc.shared === 'object' ? inc.shared : {};
  const labels: Record<string, string> = r.labels && typeof r.labels === 'object' ? r.labels : {};
  Object.keys(shared).forEach(role => parts.push(`${String(labels[role] || role)} ${String(shared[role])}`));
  const jobs = (Array.isArray(inc.job_ids) ? inc.job_ids : []).filter(Boolean).map(String);
  if (jobs.length) parts.push(`jobs ${jobs.join(', ')}`);
  return `${parts.join(' · ')}. Why did it fail, and what should L1 do?`;
}

/** "Empty" wording when a date has nothing to triage. */
export function emptyText(report: unknown): string {
  const r = obj<SupportReport>(report);
  if (r.empty && r.business_date) return `No runs for ${r.business_date} in the control table.`;
  if (r.empty) return 'No runs in the dates read.';
  return 'Nothing for L1 on this date.';
}

export const ENABLE_HINT =
  'To enable it, set SUPPORT_TABLE (project.dataset.table) and SUPPORT_COLUMNS ' +
  '(role=column, at least date, run_id and status) in your private values file; the service account needs ' +
  'BigQuery Data Viewer on that table and Job User on the project.';

// ---- "Was this right?" ------------------------------------------------------

/** The three answers, in the order the buttons show them. */
export const VERDICTS: { key: Verdict; label: string }[] = [
  { key: 'right', label: 'Right' },
  { key: 'direction', label: 'Right direction' },
  { key: 'wrong', label: 'Wrong' },
];

/** The server's cap on the cause (tools/support/feedback.MAX_CAUSE) — the input
 *  stops there so nothing the person typed is silently cut. */
export const MAX_CAUSE = 500;

export const CAUSE_PROMPT = 'What was the actual cause?';

/** Only these answers ask for the real cause: "right" has nothing to correct. */
export function asksForCause(verdict: unknown): boolean {
  return verdict === 'direction' || verdict === 'wrong';
}

/** The POST body for an investigation event ({business_date, incident_id, title,
 *  model, model_calls, seconds, …}) and the person's answer. The cause is sent
 *  as typed (trimmed) and only with an answer that asks for it. Who answered is
 *  never sent: the server takes the verified caller. */
export function feedbackPayload(data: unknown, verdict: unknown, actualCause?: unknown): FeedbackBody {
  const d = obj<InvestigationEvent>(data);
  const body: FeedbackBody = {
    business_date: text(d.business_date),
    incident_id: text(d.incident_id),
    title: text(d.title),
    verdict: text(verdict),
    actual_cause: asksForCause(verdict) ? text(actualCause).trim() : '',
    category: text(d.category),
    action: text(d.action),
    model: text(d.model),
  };
  // Timings are context, not the answer: omitted when the event had none.
  const calls = d.model_calls as unknown;
  const secs = d.seconds as unknown;
  if (calls !== null && calls !== undefined && calls !== '') body.model_calls = num(calls);
  if (secs !== null && secs !== undefined && secs !== '') body.seconds = num(secs);
  return body;
}

/** What replaces the buttons once the answer is saved. */
export function thanksText(verdict: unknown): string {
  if (verdict === 'right') return 'Thanks — marked right.';
  if (verdict === 'direction')
    return 'Thanks — marked right direction. What you wrote helps the next investigation.';
  if (verdict === 'wrong') return "Thanks — marked wrong. What you wrote is kept for the team's runbook.";
  return 'Thanks for the feedback.';
}

function percent(part: number, whole: number): string {
  return `${Math.round((100 * part) / whole)}%`;
}

/** "30 days: 42 investigations rated · 55% right · 86% right or right direction". */
export function statsLine(stats: unknown): string {
  const s = obj<FeedbackStats>(stats);
  if (s.disabled) return 'Feedback is not being kept';
  if (s.ok === false) return 'Feedback is unavailable';
  const days = num(s.days) || 30;
  const head = `${days}${days === 1 ? ' day' : ' days'}: `;
  const total = num(s.total);
  if (!total) return `${head}no investigations rated yet`;
  // The server sends ratios; fall back to the counts when only those came.
  const right =
    s.accuracy !== null && s.accuracy !== undefined ? num(s.accuracy) : num(s.right) / total;
  const near =
    s.right_or_direction !== null && s.right_or_direction !== undefined
      ? num(s.right_or_direction)
      : (num(s.right) + num(s.direction)) / total;
  return (
    `${head}${total}${total === 1 ? ' investigation' : ' investigations'} rated · ` +
    `${percent(right, 1)} right · ${percent(near, 1)} right or right direction`
  );
}
