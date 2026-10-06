/**
 * The release-history rules and wording — a 1:1 port of the portal's
 * static/core/history.js (plus `requeuePlan` and `queueDestination` from its
 * core/queue.js, which the history screen uses). Pure, so it is tested without
 * rendering anything. Which row may be ticked, what "Re-queue all" ticks and
 * what a release's header claims are decisions about the queue gate, not about
 * layout: the two UIs must agree, or one could offer a tick the gate refuses.
 *
 * A history row is in one of three states:
 *   in-queue    — already queued for the next release; cannot be ticked
 *   ready       — goes back now: it qualified once at that version (from_queue),
 *                 or it never went through the queue but already has both things
 *                 the gate asks for — the run that built it and a JIRA ticket
 *   needs-input — never queued and missing the run and/or the ticket
 */

export type HistoryItem = {
  artifact_name?: string;
  artifact_version?: string;
  jira_ticket?: string;
  build_run_url?: string;
  prl1_only?: boolean;
  df_only?: boolean;
  target_envs?: string;
  change_details?: string;
  note?: string;
  queued_by?: string;
  in_queue?: boolean;
  from_queue?: boolean;
  requeueable?: boolean;
};

export type HistoryRelease = {
  release_name?: string;
  pr_number?: number | null;
  deployment_repo?: string;
  released_at?: string;
  released_by?: string;
  items?: HistoryItem[];
};

export type ItemState = 'in-queue' | 'ready' | 'needs-input';
export type Kind = 'all' | 'CARE' | 'DF';

/** What the person typed over a needs-input row. */
export type Typed = { run?: string; jira?: string };

export type ShownRelease = {
  rel: HistoryRelease;
  index: number;
  items: Array<{ it: HistoryItem; index: number }>;
};

// The gate reads the build and its controls from a GitHub Actions run, by id;
// a looser check would enable a tick the gate then refuses.
const RUN_URL = /^https?:\/\/\S+\/actions\/runs\/\d+/;

const hasRun = (it?: HistoryItem | null) => RUN_URL.test(String(it?.build_run_url || '').trim());
const hasTicket = (it?: HistoryItem | null) => !!String(it?.jira_ticket || '').trim();

/** 'DF' for a Dataflow image (item.df_only), else 'CARE'. */
export function itemKind(it?: HistoryItem | null): 'CARE' | 'DF' {
  return it && it.df_only ? 'DF' : 'CARE';
}

/** A history row's state (see the header). */
export function itemState(it?: HistoryItem | null): ItemState {
  if (it && it.in_queue) return 'in-queue';
  if (it && it.from_queue) return 'ready';
  return hasRun(it) && hasTicket(it) ? 'ready' : 'needs-input';
}

/**
 * Filter the releases for display. query: case-insensitive substring over
 * artifact_name, artifact_version and jira_ticket (trimmed; '' matches
 * everything). Indices are the ORIGINAL positions, so ticks keyed
 * "releaseIndex:itemIndex" stay stable across filtering. A release with no
 * matching items is dropped; order is preserved (the API returns newest first).
 */
export function filterReleases(
  releases?: HistoryRelease[] | null,
  { query = '', kind = 'all' }: { query?: string; kind?: Kind | string } = {},
): ShownRelease[] {
  const q = String(query || '').trim().toLowerCase();
  const byKind = kind === 'CARE' || kind === 'DF';
  const matches = (it: HistoryItem) => {
    if (byKind && itemKind(it) !== kind) return false;
    if (!q) return true;
    return [it.artifact_name, it.artifact_version, it.jira_ticket].some(v =>
      String(v || '').toLowerCase().includes(q),
    );
  };
  const shown: ShownRelease[] = [];
  (releases || []).forEach((rel, index) => {
    const items: ShownRelease['items'] = [];
    ((rel && rel.items) || []).forEach((it, i) => {
      if (it && matches(it)) items.push({ it, index: i });
    });
    if (items.length) shown.push({ rel, index, items });
  });
  return shown;
}

/**
 * Item indices "Re-queue all" ticks for one release: every 'ready' item.
 * `onlyIndices` (the rows a search or filter leaves visible) narrows it: a
 * person must be able to see — and untick — everything the button ticks.
 */
export function requeueAllIndices(rel?: HistoryRelease | null, onlyIndices?: number[]): number[] {
  const only = onlyIndices ? new Set(onlyIndices) : null;
  const picked: number[] = [];
  ((rel && rel.items) || []).forEach((it, i) => {
    if (itemState(it) === 'ready' && (!only || only.has(i))) picked.push(i);
  });
  return picked;
}

/** What a needs-input row still lacks for the gate: [], ['run'], ['ticket'] or ['run', 'ticket']. */
export function missingInputs(it?: HistoryItem | null): Array<'run' | 'ticket'> {
  if (!it || it.in_queue || it.from_queue) return [];
  const miss: Array<'run' | 'ticket'> = [];
  if (!hasRun(it)) miss.push('run');
  if (!hasTicket(it)) miss.push('ticket');
  return miss;
}

/**
 * The row as the gate will see it: the record with anything the person typed
 * over it — the run trimmed, the ticket trimmed and upper-cased (JIRA keys
 * are). A field left untyped keeps the record's value. Always a copy.
 */
export function withTyped(it?: HistoryItem | null, typed?: Typed): HistoryItem {
  const merged: HistoryItem = { ...(it || {}) };
  if (typed && typed.run != null) merged.build_run_url = String(typed.run).trim();
  if (typed && typed.jira != null) merged.jira_ticket = String(typed.jira).trim().toUpperCase();
  return merged;
}

/** The run URL as a link target, or '' — only http(s) ever becomes a live link. */
export function safeRunHref(url?: string): string {
  const s = String(url || '').trim();
  const l = s.toLowerCase();
  return l.startsWith('https://') || l.startsWith('http://') ? s : '';
}

/**
 * The short summary on a (collapsed) release header, e.g.
 * "3 charts · 2 ready · 1 in next release" or "1 chart · needs run + ticket".
 * When one state covers every chart it reads "ready" / "all ready" instead of
 * repeating the count.
 */
export function releaseSummary(rel?: HistoryRelease | null): string {
  const items = (rel && rel.items) || [];
  const total = items.length;
  const parts = [`${total} chart${total === 1 ? '' : 's'}`];
  const count: Record<ItemState, number> = { ready: 0, 'in-queue': 0, 'needs-input': 0 };
  let missRun = false;
  let missTicket = false;
  for (const it of items) {
    const state = itemState(it);
    count[state] += 1;
    if (state === 'needs-input') {
      if (!hasRun(it)) missRun = true;
      if (!hasTicket(it)) missTicket = true;
    }
  }
  const lead = (n: number) => (n === total ? (n === 1 ? '' : 'all ') : `${n} `);
  if (count.ready) parts.push(`${lead(count.ready)}ready`);
  if (count['in-queue']) parts.push(`${lead(count['in-queue'])}in next release`);
  const n = count['needs-input'];
  if (n) {
    const what = missRun && missTicket ? 'run + ticket' : missRun ? 'run' : 'ticket';
    parts.push(`${lead(n)}${n === 1 ? 'needs ' : 'need '}${what}`);
  }
  return parts.join(' · ');
}

// ---- from the portal's core/queue.js -------------------------------------------

/** How a row's routing reads — the same words as the queue tab. */
export function queueDestination(q: HistoryItem): string {
  const picked = String(q?.target_envs || '')
    .split(',')
    .map(e => e.trim())
    .filter(Boolean);
  const envs = ['prd', 'prl1'].filter(e => picked.includes(e)).map(e => e.toUpperCase());
  if (q?.df_only) return envs.length ? `DF → ${envs.join(', ')}` : 'DF (Dataflow)';
  return q?.prl1_only ? 'CARE → UAT, PRL1' : 'CARE → UAT, PRL1, PRD';
}

export type DirectItem = { artifact_name: string; artifact_version: string };
export type GatedRow = {
  artifact: string;
  build_run_url: string;
  jira_ticket: string;
  prl1_only: boolean;
  df_only: boolean;
  target_envs: string;
  change_details: string;
  note: string;
};
export type Skipped = { artifact: string; reason: string };

/**
 * What to do with the ticked charts. One that went through the gate once (at
 * that version) goes back DIRECTLY (/api/release-queue/requeue). One that never
 * went through the queue never qualified: it takes the gate like a first
 * submission (/api/release-queue/batch) — only once it has a run and a ticket.
 * Whatever cannot go either way is in `skipped` with the reason.
 */
export function requeuePlan(items?: HistoryItem[] | null): {
  direct: DirectItem[];
  gated: GatedRow[];
  skipped: Skipped[];
} {
  const direct: DirectItem[] = [];
  const gated: GatedRow[] = [];
  const skipped: Skipped[] = [];
  for (const it of items || []) {
    const artifact = `${it.artifact_name}:${it.artifact_version || ''}`;
    if (it.in_queue) {
      skipped.push({ artifact, reason: 'already queued for the next release' });
      continue;
    }
    if (it.from_queue) {
      direct.push({ artifact_name: String(it.artifact_name), artifact_version: it.artifact_version || '' });
      continue;
    }
    if (!it.build_run_url) {
      skipped.push({
        artifact,
        reason: 'never went through the queue — paste the run that built it in its row, then tick',
      });
      continue;
    }
    if (!it.jira_ticket) {
      skipped.push({
        artifact,
        reason: 'never went through the queue — the gate needs a JIRA ticket; add it in its row, then tick',
      });
      continue;
    }
    gated.push({
      artifact,
      build_run_url: it.build_run_url,
      jira_ticket: it.jira_ticket || '',
      prl1_only: !!it.prl1_only,
      df_only: !!it.df_only,
      target_envs: it.target_envs || '',
      change_details: it.change_details || '',
      note: it.note || '',
    });
  }
  return { direct, gated, skipped };
}

// ---- the screen's sentences (static/forms/release_history.js) ------------------

/** How far back the history reads. */
export const HISTORY_DAYS = 21;

export const HISTORY_HINT =
  'Tick the charts to put back into the next release, or use “Re-queue all” on a release to tick ' +
  'every one that can go. A chart that came through the queue goes straight back — it already qualified; ' +
  'one that never did needs its run and ticket.';

/** "3 releases in the last 3 weeks". */
export function releasesCount(n: number): string {
  return `${n} release${n === 1 ? '' : 's'} in the last 3 weeks`;
}

/** Why a needs-input row's tick is disabled. */
export function missingWhy(it: HistoryItem): string {
  const miss = missingInputs(it);
  if (miss.length === 2) {
    return 'Never went through the queue — paste the run that built it and its JIRA ticket, then tick';
  }
  if (miss[0] === 'run') {
    return 'Never went through the queue — paste the GitHub Actions run that built it (…/actions/runs/<id>), then tick';
  }
  return 'Never went through the queue — add its JIRA ticket, then tick';
}

/** Why "Re-queue all" is disabled on a release. */
export function noneWhy(rel: HistoryRelease): string {
  return ((rel && rel.items) || []).every(it => itemState(it) === 'in-queue')
    ? 'Every chart in this release is already in the next release'
    : 'None can go straight back: a chart that never went through the queue needs its run and ticket first — ' +
        'add them in its row, then tick it';
}

/** The "Re-queue all" button's title. */
export function requeueAllTitle(n: number, rel: HistoryRelease): string {
  if (!n) return noneWhy(rel);
  return n === 1
    ? 'Tick the 1 chart that can go straight back — nothing is sent until you add it below'
    : `Tick the ${n} charts that can go straight back — nothing is sent until you add them below`;
}

/** Where a row came from, as the row's tooltip says. */
export function rowOrigin(it: HistoryItem): string {
  if (it.queued_by) return `Queued by ${it.queued_by}`;
  return !it.from_queue && !it.in_queue
    ? 'Never went through the queue — typed straight into a release form, so the gate never ran for it'
    : '';
}

/** The submit button's label: how many ticks, and how many the filter hides. */
export function addLabel(ticked: number, hidden: number): string {
  if (!ticked) return 'Add selected to the next release';
  return `Add ${ticked} to the next release${hidden ? ` (${hidden} hidden by the filter)` : ''}`;
}

/** The submit button while the calls run. */
export function busyLabel(gated: number): string {
  if (!gated) return 'Putting back…';
  return gated > 1 ? `Checking ${gated} builds…` : 'Checking the build…';
}

/** The list when the search or filter leaves nothing. */
export function emptyListText(query: string, kind: Kind | string): string {
  const q = String(query || '').trim();
  const what = kind === 'all' ? 'chart' : `${kind} chart`;
  return q
    ? `No ${what} matches “${q}” in the last 3 weeks.`
    : `No ${what} shipped in the last 3 weeks.`;
}

/** The tooltip on a kind filter chip. */
export function kindTitle(kind: Kind, n: number): string {
  const what = kind === 'all' ? 'Every chart' : kind === 'DF' ? 'Dataflow images only' : 'CARE charts only';
  return `${what} — ${n} in the last 3 weeks`;
}

/** How many charts of a kind the releases hold (the chip counts). */
export function kindCount(releases: HistoryRelease[], kind: Kind): number {
  return releases.reduce(
    (n, rel) => n + ((rel && rel.items) || []).filter(it => kind === 'all' || itemKind(it) === kind).length,
    0,
  );
}

/** The history could not be read. */
export function unavailableText(ctx: { disabled?: boolean; error?: string }): string {
  return ctx.disabled
    ? 'The history is off — no BigQuery dataset is configured.'
    : `The history is unavailable: ${ctx.error || 'unknown error'}`;
}

export const NO_RELEASES = 'No release has shipped from the queue in the last 3 weeks.';
export const EMAIL_NEEDED = 'Your email is needed — the queue records who asked.';
export const COULD_NOT_QUEUE = 'Could not queue — try again.';
