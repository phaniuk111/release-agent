/**
 * Wording for the Monitoring tab — a 1:1 port of the portal's pure rules in
 * static/core/bq_cost.js (the BigQuery cost report) and static/core/monitoring.js
 * (the PromQL checks); their tests are tests/js/bq_cost.test.mjs and
 * tests/js/monitoring.test.mjs. The server measures and decides (tools/bq_cost.py,
 * tools/monitoring.py); this only says what it found. Every function takes a
 * payload with fields missing and still answers — a half-built report is shown,
 * not a blank card. Sentences are copied exactly: change them in both places.
 */
import { shortName } from './queueFormat';

// ---- payloads (every field optional: the server's dicts, as they arrive) ----

export type BqShape = {
  qhash?: string;
  runs?: number | null;
  who?: string | null;
  users?: (string | null)[] | null;
  users_count?: number | null;
  slot_hours?: number | null;
  gb_billed?: number | null;
  approx_usd?: number | null;
  p50_bytes?: number | null;
  p50_gb?: number | null;
  sql_preview?: string | null;
  insights?: unknown;
  [key: string]: unknown;
};

export type BqMissingAccess = {
  role?: string;
  grant_on?: string;
  permission?: string;
  sections?: string[];
  note?: string;
};

export type BqHistory = {
  adoption?: { status?: string }[];
  adopted?: number | null;
  still_open?: number | null;
  new?: number | null;
  gone?: number | null;
};

export type BqReport = {
  ok?: boolean;
  disabled?: boolean;
  error?: string;
  hint?: string | null;
  status?: number;
  project?: string;
  region?: string;
  days?: number | null;
  billing?: string;
  scanned_at?: string;
  report_cost_bytes?: number | null;
  totals?: {
    queries?: number | null;
    slot_hours?: number | null;
    gb_billed?: number | null;
    cache_hit_pct?: number | null;
  } | null;
  shapes?: BqShape[] | null;
  shapes_error?: string | null;
  storage?: unknown[] | null;
  storage_error?: string | null;
  writes?: unknown[] | null;
  writes_error?: string | null;
  history?: BqHistory | null;
  missing_access?: BqMissingAccess[] | null;
  [key: string]: unknown;
};

export type CheckState = 'firing' | 'unknown' | 'ok' | 'no_data';

export type MonitorSeries = {
  labels?: Record<string, string>;
  value?: unknown;
};

export type MonitorCheck = {
  name: string;
  state: CheckState | string;
  query?: string;
  description?: string;
  severity?: string;
  count?: number;
  watching?: number | null;
  error?: string;
  hint?: string;
  series?: MonitorSeries[];
};

export type MonitorResult = {
  ok?: boolean;
  error?: string;
  source?: string;
  checked_at?: string;
  config_error?: string | null;
  checks?: MonitorCheck[] | null;
};

// ---- BigQuery cost ------------------------------------------------------------

const K = 1024; // bytes per KB, MB per GB, GB per TB, TB per PB

/** A finite number, or null for anything that is not one (null, '', 'n/a'). */
function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** An array, or [] for anything else — a payload's list is never assumed. */
function list<T = unknown>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

function obj<T extends object>(v: unknown): Partial<T> {
  return v && typeof v === 'object' ? (v as Partial<T>) : {};
}

/** Three figures are enough for a size: 12.6, 84.2, 380. */
function trim(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 100) return formatCount(value);
  if (abs >= 0.1) return value.toFixed(1);
  return value.toFixed(2);
}

function plural(n: unknown, one: string, many: string): string {
  return num(n) === 1 ? one : many;
}

/** 1240 → "1,240". Hand-rolled so the text is the same in every locale. */
export function formatCount(n: unknown): string {
  const v = num(n);
  if (v === null) return '—';
  const r = Math.round(v);
  const s = String(Math.abs(r));
  let out = '';
  for (let i = 0; i < s.length; i++) {
    if (i && (s.length - i) % 3 === 0) out += ',';
    out += s[i];
  }
  return (r < 0 ? '-' : '') + out;
}

/** A size in the unit that reads best: 0.0123 → "12.6 MB", 380.1 → "380 GB",
 * 18432 → "18.0 TB". Bytes billed are what BigQuery charges for, so the unit
 * follows the figure rather than the other way round. */
export function formatGb(gb: unknown): string {
  const n = num(gb);
  if (n === null) return '—';
  if (n === 0) return '0 GB';
  const abs = Math.abs(n);
  if (abs >= K * K) return `${trim(n / (K * K))} PB`;
  if (abs >= K) return `${trim(n / K)} TB`;
  if (abs < 0.1) return `${trim(n * K)} MB`;
  return `${trim(n)} GB`;
}

/** A byte count in the unit that reads best — the server's GB figure rounds a
 * 21 KB query to 0.0, so screens prefer this whenever raw bytes are present:
 * 21557 → "21.1 KB", 13200000 → "12.6 MB", 0 → "0 B". */
export function formatBytes(bytes: unknown): string {
  const n = num(bytes);
  if (n === null) return '—';
  const abs = Math.abs(n);
  if (abs < K) return `${formatCount(n)} B`;
  if (abs < K * K) return `${trim(n / K)} KB`;
  return formatGb(n / (K * K * K));
}

/** "$0.01", "$112.50", "$1,234.50"; null → "—". `decimals` 0 for a monthly figure. */
export function formatUsd(usd: unknown, decimals = 2): string {
  const n = num(usd);
  if (n === null) return '—';
  const fixed = Math.abs(n).toFixed(decimals);
  const dot = fixed.indexOf('.');
  const whole = dot === -1 ? fixed : fixed.slice(0, dot);
  const frac = dot === -1 ? '' : fixed.slice(dot);
  return `${n < 0 ? '-' : ''}$${formatCount(whole)}${frac}`;
}

/** The line under the heading: "1,240 queries · 612 slot-hours · 84.2 GB billed ·
 * this report read 0.4 GB". Parts the scan did not measure are left out;
 * a disabled or failed report says so instead of showing zeros — the failure's
 * own text belongs in the card body, not repeated in the heading. */
export function costSummary(report: BqReport | null | undefined): string {
  if (!report || typeof report !== 'object') return 'no report';
  if (report.disabled) return 'BigQuery cost report is disabled';
  if (report.ok === false) return 'BigQuery cost report is unavailable';
  const t = obj<NonNullable<BqReport['totals']>>(report.totals);
  const parts: string[] = [];
  // Job history refused: the totals are unknown, not zero.
  if (report.shapes_error)
    parts.push('query costs not readable with this access');
  else if (num(t.queries) !== null)
    parts.push(
      formatCount(t.queries) + plural(t.queries, ' query', ' queries'),
    );
  if (!report.shapes_error && num(t.slot_hours) !== null)
    parts.push(`${trim(num(t.slot_hours)!)} slot-hours`);
  if (!report.shapes_error && num(t.gb_billed) !== null)
    parts.push(`${formatGb(t.gb_billed)} billed`);
  if (!report.shapes_error && num(t.cache_hit_pct) !== null)
    parts.push(`${trim(num(t.cache_hit_pct)!)}% from cache`);
  if (num(report.report_cost_bytes) !== null) {
    parts.push(
      `this report read ${formatBytes(num(report.report_cost_bytes))}`,
    );
  }
  return parts.join(' · ');
}

/** "my-project · region-us · last 14 days · on-demand billing · scanned …". */
export function reportMeta(report: BqReport | null | undefined): string {
  const r = obj<BqReport>(report);
  const parts: string[] = [];
  if (r.project) parts.push(String(r.project));
  if (r.region) parts.push(String(r.region));
  if (num(r.days) !== null)
    parts.push(`last ${formatCount(r.days)}${plural(r.days, ' day', ' days')}`);
  if (r.billing) parts.push(`${String(r.billing)} billing`);
  if (r.scanned_at) parts.push(`scanned ${String(r.scanned_at)}`);
  return parts.join(' · ');
}

/** "3 adopted · 2 still open" from the memory across runs (design §7) — the
 * counts, given either as numbers or as an adoption list to count; "" without. */
export function historyText(history: BqHistory | null | undefined): string {
  if (!history || typeof history !== 'object') return '';
  const counts: Record<'adopted' | 'still_open' | 'new' | 'gone', number> = {
    adopted: 0,
    still_open: 0,
    new: 0,
    gone: 0,
  };
  type Key = keyof typeof counts;
  list<{ status?: string }>(history.adoption).forEach(a => {
    if (
      a &&
      typeof a.status === 'string' &&
      Object.prototype.hasOwnProperty.call(counts, a.status)
    ) {
      counts[a.status as Key] += 1;
    }
  });
  (Object.keys(counts) as Key[]).forEach(k => {
    const v = num(history[k]);
    if (v !== null) counts[k] = v;
  });
  const words: Record<Key, string> = {
    adopted: 'adopted',
    still_open: 'still open',
    new: 'new',
    gone: 'gone',
  };
  return (Object.keys(counts) as Key[])
    .filter(k => counts[k] > 0)
    .map(k => `${formatCount(counts[k])} ${words[k]}`)
    .join(' · ');
}

/** What the table does not show but the Excel does: "storage: 3 findings ·
 * writes: unavailable · history: 2 adopted, 1 still open — in the Excel".
 * "" when there is nothing beyond the table. */
export function extrasText(report: BqReport | null | undefined): string {
  const r = obj<BqReport>(report);
  const parts: string[] = [];
  const section = (name: string, items: unknown, error: unknown) => {
    const n = list(items).length;
    if (n)
      parts.push(
        `${name}: ${formatCount(n)}${plural(n, ' finding', ' findings')}`,
      );
    else if (error) parts.push(`${name}: unavailable`);
  };
  section('storage', r.storage, r.storage_error);
  section('writes', r.writes, r.writes_error);
  const h = historyText(r.history);
  if (h) parts.push(`history: ${h.split(' · ').join(', ')}`);
  return parts.length ? `${parts.join(' · ')} — in the Excel` : '';
}

/** The lead line of a partial report's "access needed" panel. */
export const PARTIAL_LEAD =
  "Partial report — this account can't read every section. " +
  'Grant these to see the rest:';

export type AccessLine = {
  role: string;
  grantOn: string;
  permission: string;
  unlocks: string;
  note: string;
};

/** The roles a partial report is missing, one per role and where to grant it:
 * {role, grantOn, permission, unlocks, note}. [] when nothing is missing. */
export function accessLines(report: BqReport | null | undefined): AccessLine[] {
  const r = obj<BqReport>(report);
  return list<BqMissingAccess>(r.missing_access)
    .filter(a => a && typeof a === 'object' && a.role)
    .map(a => ({
      role: String(a.role),
      grantOn: String(a.grant_on || ''),
      permission: String(a.permission || ''),
      unlocks: list(a.sections).join('; '),
      note: String(a.note || ''),
    }));
}

/** A window with nothing worth a row. */
export function emptyText(report: BqReport | null | undefined): string {
  if (report && report.shapes_error) {
    return 'query costs unavailable — this account needs more access (see above)';
  }
  const d = num(report && report.days);
  return d === null
    ? 'nothing significant in the window'
    : `nothing significant in the last ${formatCount(d)}${plural(
        d,
        ' day',
        ' days',
      )}`;
}

/** How to turn the report on — the config names, since that is what someone
 * has to set (helm values.yaml → config:). The roles are design §2. */
export const ENABLE_HINT =
  'To enable it, set BQ_COST_REGION to the INFORMATION_SCHEMA region ' +
  '(e.g. region-europe-west3) and BQ_COST_PROJECT (or BQ_PROJECT) to the project to scan; ' +
  'the service account needs bigquery.resourceViewer, metadataViewer and jobUser there — ' +
  'never dataViewer.';

/** Biggest first: slot-hours on reservations (what the team pays for), bytes
 * billed on-demand. Stable, so equal shapes keep the server's order. */
export function orderShapes(shapes: unknown, billing: unknown): BqShape[] {
  const key = billing === 'reservations' ? 'slot_hours' : 'gb_billed';
  const of = (s: BqShape) => num(s && s[key]) || 0;
  return list<BqShape>(shapes)
    .map((s, i) => [s, i] as const)
    .sort((a, b) => of(b[0]) - of(a[0]) || a[1] - b[1])
    .map(([s]) => s);
}

/** Who runs a shape, in the tight column: "alice", or "alice +2" when others do
 * too. `users` is a sample of the emails (the server caps it); `users_count`
 * is the real distinct count, so it decides the "+N" when present. */
export function whoText(shape: BqShape | null | undefined): string {
  const s = obj<BqShape>(shape);
  const who = String(s.who || list<string>(s.users)[0] || '');
  const name = shortName(who);
  const count = num(s.users_count);
  const others =
    count !== null
      ? Math.max(0, count - 1)
      : list<string>(s.users).filter(u => u && u !== who).length;
  return name + (others ? ` +${others}` : '');
}

/** BigQuery's performance insights in words: ["slot_contention"] → "slot contention",
 * joined with ", "; nothing → "". Repeats (one per stage) are said once. */
export function insightText(insights: unknown): string {
  const seen = new Set<string>();
  const words: string[] = [];
  list(insights).forEach(i => {
    const raw =
      i && typeof i === 'object'
        ? (i as Record<string, unknown>).name ||
          (i as Record<string, unknown>).kind ||
          (i as Record<string, unknown>).type ||
          ''
        : i;
    const w = String(raw === null || raw === undefined ? '' : raw)
      .split('_')
      .join(' ')
      .trim();
    if (w && !seen.has(w)) {
      seen.add(w);
      words.push(w);
    }
  });
  return words.join(', ');
}

/** What the chat is asked when someone clicks "Ask why" on a row — the model
 * does the investigating (bq_query_detail, the tables) and proves any rewrite
 * with bq_verify_rewrite; this only names the shape and its cost.
 * (`explainPrompt` in static/core/bq_cost.js.) */
export function shapeExplainPrompt(shape: BqShape | null | undefined): string {
  const s = obj<BqShape>(shape);
  const facts: string[] = [];
  if (num(s.runs) !== null)
    facts.push(formatCount(s.runs) + plural(s.runs, ' run', ' runs'));
  if (num(s.gb_billed) !== null) facts.push(`${formatGb(s.gb_billed)} billed`);
  return (
    `Why is BigQuery query shape ${String(s.qhash || '?')} expensive${
      facts.length ? ` (${facts.join(', ')})` : ''
    }? Use bq_query_detail, look at the referenced tables, and propose a cheaper rewrite ` +
    'tested with bq_verify_rewrite.'
  );
}

// ---- PromQL checks --------------------------------------------------------------

/** "2 firing · 3 OK · 4 not measured here" — every state named, so a
 * project with nothing to measure never reads as "all clear". */
export function monitoringSummary(
  result: MonitorResult | null | undefined,
): string {
  const checks = (result && result.checks) || [];
  if (!checks.length) return 'no checks configured';
  const n = (state: string) => checks.filter(c => c.state === state).length;
  const parts = [
    n('firing') ? `${n('firing')} firing` : '',
    n('unknown') ? `${n('unknown')} could not run` : '',
    n('ok') ? `${n('ok')} OK` : '',
    n('no_data') ? `${n('no_data')} not measured here` : '',
  ].filter(Boolean);
  if (!n('firing') && !n('unknown') && !n('no_data')) {
    return n('ok') === 1 ? 'the check is OK' : `all ${n('ok')} checks OK`;
  }
  return parts.join(' · ');
}

/** Firing first, then broken, then healthy, then unmeasured — what needs a
 * human is at the top. Stable within a state. */
export function orderChecks(
  checks: MonitorCheck[] | null | undefined,
): MonitorCheck[] {
  const rank: Record<string, number> = {
    firing: 0,
    unknown: 1,
    ok: 2,
    no_data: 3,
  };
  const of = (c: MonitorCheck) => rank[c.state] ?? 1;
  return (checks || [])
    .map((c, i) => [c, i] as const)
    .sort((a, b) => of(a[0]) - of(b[0]) || a[1] - b[1])
    .map(([c]) => c);
}

/** "watching 17" for a healthy check that measured something. */
export function watchingText(
  check: Pick<MonitorCheck, 'watching'> | null | undefined,
): string {
  return check && typeof check.watching === 'number' && check.watching > 0
    ? `watching ${check.watching}`
    : '';
}

/** A series' labels, most telling first, without Prometheus' own bookkeeping. */
export function seriesLabel(
  labels: Record<string, string> | null | undefined,
): string {
  const skip = new Set([
    '__name__',
    'monitored_resource',
    'project_id',
    'location',
  ]);
  const entries = Object.entries(labels || {}).filter(([k]) => !skip.has(k));
  if (!entries.length) return (labels && labels.__name__) || 'value';
  return entries.map(([k, v]) => `${k}=${v}`).join(', ');
}

/** 63 → "63", 0.000034722 → "3.47e-5", 12.5 → "12.5". */
export function formatValue(v: unknown): string {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  if (Number.isInteger(n)) return String(n);
  if (Math.abs(n) < 0.001) return n.toExponential(2);
  return String(Math.round(n * 100) / 100);
}

/** What the chat is asked when someone clicks "Ask why" on a check.
 * (`explainPrompt` in static/core/monitoring.js.) */
export function checkExplainPrompt(
  check: Pick<MonitorCheck, 'name' | 'state' | 'query'>,
): string {
  return (
    `The monitoring check "${check.name}" is ${check.state}` +
    ` (PromQL: ${check.query}). Show me what it found and help me work out why.`
  );
}

/** The checks' state words and the portal's colours for them. */
export const CHECK_STATE: Record<
  CheckState,
  { text: string; tone: 'red' | 'amber' | 'emerald' | 'faint' }
> = {
  firing: { text: 'firing', tone: 'red' },
  unknown: { text: 'could not run', tone: 'amber' },
  ok: { text: 'OK', tone: 'emerald' },
  no_data: { text: 'not measured', tone: 'faint' },
};

/** Every check is in a project with nothing to measure. */
export const NOTHING_MEASURED =
  'Nothing these checks watch reports into this ' +
  'project — the metrics may live in another one (set PROMETHEUS_PROJECT), or add checks for the ' +
  "team's own metrics (MONITOR_CHECKS).";

/** The fold that holds the checks with no data. */
export function unmeasuredText(n: number): string {
  return `${n} not measured here — no data for them in this project`;
}

/** How to apply the alert policy the portal builds (it changes nothing itself). */
export const ALERT_POLICY_LEAD =
  'Save as policy.json, add your notification channels, then apply it once — ' +
  'Cloud Monitoring checks it every minute and notifies them:';
export const ALERT_POLICY_COMMAND =
  'gcloud alpha monitoring policies create --policy-from-file=policy.json';
