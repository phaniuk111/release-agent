/**
 * The deploy and release forms' pure rules — ports of the portal's
 * static/core/chg.js, the release bits of static/core/queue.js, and the
 * parsing and validation inside static/forms/release_form.js and
 * forms/parse.js (parseDeployInclude). Same wording, same payloads: the chat
 * router parses what these build, so a drift here is a refusal there.
 */

// ---- change-request fields (core/chg.js) ------------------------------------

/** The release file's prose keys, in the file's own order. */
export const PROSE_FIELDS = [
  'change_description',
  'change_reason',
  'associated_risk',
  'consequence',
  'user_service_impact',
] as const;

/** A DF release drafts its summary too (a CARE mono summary is the release name). */
export const DF_DRAFT_FIELDS = ['change_summary', ...PROSE_FIELDS] as const;

export type DraftField = (typeof DF_DRAFT_FIELDS)[number];
export type ChipState = '' | 'ai' | 'team' | 'fallback' | 'edited';
/** The last automatic write into a field: its text and who wrote it. */
export type AutoWrite = { value: string; source: string } | null | undefined;

/** What each chip says, and its tooltip. */
export const CHIPS: Record<
  Exclude<ChipState, ''>,
  { label: string; title: string }
> = {
  ai: {
    label: 'AI draft',
    title:
      "Summarised by the model from the developers' queue entries — review it",
  },
  team: {
    label: 'Team wording',
    title: "The team's standard wording, built from the queued facts",
  },
  edited: { label: 'Edited', title: 'Your text — Regenerate leaves it alone' },
  fallback: {
    label: 'Fallback',
    title:
      'The draft for this field came back empty or too long, so the standard wording is used',
  },
};

const norm = (v: unknown) => String(v ?? '').trim();

/**
 * May an automatic value replace what the field holds? Yes when the field is
 * empty or still holds the last automatic value (compared trimmed) — i.e. the
 * person has not edited it.
 */
export function canReplace(value: string, auto: AutoWrite): boolean {
  const now = norm(value);
  return !now || (!!auto && norm(auto.value) === now);
}

/** The chip beside a prose field: empty, edited, or who wrote it last. */
export function chipState(value: string, auto: AutoWrite): ChipState {
  const now = norm(value);
  if (!now) return '';
  if (!auto || norm(auto.value) !== now) return 'edited';
  return auto.source === 'team' || auto.source === 'fallback'
    ? auto.source
    : 'ai';
}

/**
 * One key per set of artifact lines — a draft made for one key is stale for
 * any other. Each line counts by its last path segment (name:version); blank
 * lines, trailing slashes, repeats and order do not matter.
 */
export function itemsKey(lines: string[] | null | undefined): string {
  const seen = new Set<string>();
  (lines ?? []).forEach(line => {
    let s = norm(line);
    while (s.endsWith('/')) s = s.slice(0, -1);
    const last = (s.split('/').pop() ?? '').trim();
    if (last) seen.add(last);
  });
  return Array.from(seen).sort().join('\n');
}

/** The CARE form's header in mono mode. */
export function monoReleaseNote(file?: string, repo?: string): string {
  return (
    `One file — ${norm(file) || 'the release file'} in ${
      norm(repo) || 'the release repo (CARE_RELEASE_REPO is not set)'
    }. The portal raises a PR for you to review and merge; ` +
    "you'll see the exact change before anything is pushed."
  );
}

// ---- queued rows inside a release (core/queue.js) ---------------------------

export type QueuedItem = {
  artifact_name?: string;
  artifact_version?: string;
  requested_by?: string;
  build_verified?: boolean | null;
  prl1_only?: boolean;
  df_only?: boolean;
  target_envs?: string;
  jira_ticket?: string;
  change_details?: string;
  note?: string;
};

/** The environments a queued row names, e.g. ["prd", "prl1"]. */
export function envsOf(q?: QueuedItem | null): string[] {
  return String(q?.target_envs ?? '')
    .split(',')
    .map(e => e.trim())
    .filter(Boolean);
}

/** How a queued row reads inside a release form of that kind (routing only). */
export function releaseRouteText(q: QueuedItem, isDf: boolean): string {
  if (isDf) {
    const envs = ['prd', 'prl1']
      .filter(e => envsOf(q).includes(e))
      .map(e => e.toUpperCase());
    return envs.length
      ? `${envs.join(' + ')} pipeline${
          envs.length > 1 ? 's' : ''
        } — triggered at deploy time`
      : 'pipelines chosen at deploy time';
  }
  return q.prl1_only
    ? 'UAT → PRL1 · PRL1-only, held back from PRD'
    : 'UAT → PRL1 → PRD';
}

/** Queued rows belonging to one release: a DF release carries only DF images. */
export function forRelease<T extends QueuedItem>(
  queue: T[] | null | undefined,
  isDf: boolean,
): T[] {
  return (queue ?? []).filter(q => (isDf ? !!q.df_only : !q.df_only));
}

/** The build badge a queued row carries — nothing is shown for 'unknown'. */
export function buildSummary(q?: QueuedItem | null): {
  state: 'verified' | 'unverified' | 'unknown';
  label: string;
  title: string;
} {
  const v = q ? q.build_verified : null;
  if (v === true) {
    return {
      state: 'verified',
      label: 'verified',
      title:
        'traced to the GitHub Actions run that built this version, at queue time',
    };
  }
  if (v === false) {
    return {
      state: 'unverified',
      label: 'not verified',
      title: 'no build run could be traced to this version at queue time',
    };
  }
  return {
    state: 'unknown',
    label: 'not checked',
    title: 'the build was not checked when this was queued',
  };
}

/** Why the checklist is empty — and where the items went, when the other release has them. */
export function emptyQueueNote(isDf: boolean, otherQueued: number): string {
  const kind = isDf ? 'Dataflow' : 'CARE';
  const other = isDf ? 'CARE' : 'Dataflow';
  const where = otherQueued
    ? ` — ${otherQueued} item${
        otherQueued === 1 ? ' is' : 's are'
      } queued for the ${other} release`
    : '';
  const what = isDf ? 'Dataflow images' : 'charts';
  return `Nothing is queued for the ${kind} release yet${where}. Developers queue ${what} with Add to next release, ticking ${kind}. You can still type artifacts below.`;
}

// ---- the artifact lines (forms/release_form.js) ------------------------------

/** The artifact box's non-blank lines, trimmed. */
export function artifactLines(text: string): string[] {
  return String(text ?? '')
    .split('\n')
    .map(l => l.trim())
    .filter(Boolean);
}

/** Each line's chart name: name:version or a full registry URL ending in one. */
export function artifactNames(text: string): string[] {
  return artifactLines(text)
    .map(line => {
      let l = line;
      while (l.endsWith('/')) l = l.slice(0, -1);
      const last = l.split('/').pop() ?? '';
      const i = last.indexOf(':');
      return i > 0 ? last.slice(0, i) : null;
    })
    .filter((n): n is string => !!n);
}

/** The artifact box with a queued item ticked (added, replacing any version of it) or unticked. */
export function withQueuedItem(
  text: string,
  q: QueuedItem,
  on: boolean,
): string {
  const prefix = `${q.artifact_name}:`;
  const lines = artifactLines(text).filter(
    l => (l.split('/').pop() ?? '').indexOf(prefix) !== 0,
  );
  if (on) lines.push(`${q.artifact_name}:${q.artifact_version}`);
  return lines.join('\n');
}

/** PRL1-only names: the queued choice, else the manual box (a DF release has none). */
export function prl1Names(
  text: string,
  queuedByName: Record<string, QueuedItem>,
  manual: Set<string>,
  isDf: boolean,
): string[] {
  if (isDf) return [];
  return Array.from(new Set(artifactNames(text))).filter(n =>
    queuedByName[n] ? !!queuedByName[n].prl1_only : manual.has(n),
  );
}

/** A datetime-local value as the release sends it: "2026-10-08 18:00:00". */
export function releaseDateTime(v: string): string {
  return v ? v.replace('T', ' ') + (v.length === 16 ? ':00' : '') : '';
}

/** Today in the browser's zone, YYYY-MM-DD — the defaults' date before a start is set. */
export function localToday(d: Date = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(
    2,
    '0',
  )}-${String(d.getDate()).padStart(2, '0')}`;
}

export type ReleaseFields = {
  name: string;
  jira: string;
  start: string;
  end: string;
  initiator: string;
  summary: string;
  description: string;
  reason: string;
  risk: string;
  consequence: string;
  impact: string;
  repo: string;
  artifacts: string;
};

/** The first thing stopping a submit, in the portal's order; '' when none. */
export function releaseProblem(f: ReleaseFields, jiraProblem: string): string {
  if (
    !f.name.trim() ||
    !f.start ||
    !f.end ||
    !f.initiator.trim() ||
    !f.summary.trim()
  ) {
    return 'Release name, start, end, initiator and summary are required.';
  }
  if (jiraProblem) return jiraProblem;
  if (!f.repo.trim() || f.repo.indexOf('/') < 1)
    return 'Deployment repo is required (owner/repo).';
  if (!artifactLines(f.artifacts).length)
    return 'At least one artifact is required.';
  if (releaseDateTime(f.end) <= releaseDateTime(f.start))
    return 'End must be after start.';
  return '';
}

/** Exactly what release_form.js sends through the chat, key for key. */
export function releasePayload(
  f: ReleaseFields,
  opts: { isDf: boolean; jira: string; prl1Only: string[] },
) {
  return {
    deployment_repo: f.repo.trim(),
    release_name: f.name.trim(),
    start_date: releaseDateTime(f.start),
    end_date: releaseDateTime(f.end),
    change_initiator: f.initiator.trim(),
    jira: opts.jira,
    change_summary: f.summary.trim(),
    change_description: f.description.trim(),
    change_reason: f.reason.trim(),
    associated_risk: f.risk.trim(),
    consequence: f.consequence.trim(),
    user_service_impact: f.impact.trim(),
    prl1_only: opts.prl1Only,
    // A DF release is all DF images; a CARE release carries none.
    df_images: opts.isDf ? Array.from(new Set(artifactNames(f.artifacts))) : [],
    artefact: artifactLines(f.artifacts),
    release_kind: opts.isDf ? 'df' : 'care',
  };
}

// ---- the deploy editor (forms/parse.js) ---------------------------------------

/**
 * Every balanced {...} in the text that parses on its own and looks like a
 * chart entry — the recovery when the box is not valid JSON (objects pasted
 * with no commas and no include[] wrapper).
 */
function extractJsonObjects(text: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== '{') continue;
    let depth = 0;
    let inStr = false;
    let esc = false;
    let end = -1;
    for (let j = i; j < text.length; j++) {
      const ch = text[j];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === '\\') esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === '{') depth++;
      else if (ch === '}') {
        depth--;
        if (depth === 0) {
          end = j;
          break;
        }
      }
    }
    if (end === -1) break;
    try {
      const e = JSON.parse(text.slice(i, end + 1));
      if (
        e &&
        typeof e === 'object' &&
        !Array.isArray(e) &&
        (e.helm_chart_name !== undefined || e.helm_chart_version !== undefined)
      ) {
        out.push(e);
      }
    } catch {
      /* this {...} isn't a standalone object — skip */
    }
  }
  return out;
}

/**
 * The deploy editor's entries: a clean {"include":[...]}, a bare array, or a
 * single entry; else every chart-shaped object recovered from the text.
 * null when nothing is found.
 */
export function parseDeployInclude(
  input: string,
): { include: unknown[]; recovered: boolean } | null {
  const text = String(input ?? '').trim();
  try {
    const doc = JSON.parse(text);
    if (Array.isArray(doc)) return { include: doc, recovered: false };
    if (doc && Array.isArray(doc.include))
      return { include: doc.include, recovered: false };
    if (doc && typeof doc === 'object' && doc.helm_chart_name !== undefined) {
      return { include: [doc], recovered: false };
    }
  } catch {
    /* fall through to lenient recovery */
  }
  const entries = extractJsonObjects(text);
  return entries.length ? { include: entries, recovered: true } : null;
}

/** Why the deploy editor's entries cannot be sent, in the portal's words; '' when they can. */
export function deployIncludeProblem(
  parsed: { include: unknown[] } | null,
): string {
  if (!parsed || !parsed.include.length) {
    return 'Could not find any chart entries — each needs helm_chart_name + helm_chart_version.';
  }
  for (const it of parsed.include) {
    const e = it as {
      helm_chart_name?: unknown;
      helm_chart_version?: unknown;
    } | null;
    if (!e || !e.helm_chart_name || !e.helm_chart_version) {
      return 'Each entry needs a non-empty helm_chart_name + helm_chart_version.';
    }
  }
  return '';
}
