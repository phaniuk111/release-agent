// The release-history rules both UIs must agree on. Pure: no DOM, no fetch — the
// Node tests import it directly and a React port (the Backstage plugin
// included) can use it as is. Which row may be ticked, what "Re-queue all"
// ticks and what a release's header claims are decisions about the queue gate,
// not about layout: drawn separately, one screen could offer a tick that the
// gate then refuses, or count as ready a chart the other calls blocked.
//
// A history row is in one of three states:
//   in-queue    — already queued for the next release; ticking it again would
//                 only be skipped, so it cannot be ticked
//   ready       — goes back now: it qualified once at that version (from_queue),
//                 or it never went through the queue but already has both things
//                 the gate asks for — the run that built it and a JIRA ticket
//   needs-input — never queued and missing the run and/or the ticket
// A screen that lets the person type a missing run or ticket passes the row
// with the typed values merged in, and the state follows.

// The gate reads the build and its controls from a GitHub Actions run, by id;
// a looser check would enable a tick the gate then refuses.
const RUN_URL = /^https?:\/\/\S+\/actions\/runs\/\d+/;

const hasRun = (it) => RUN_URL.test(String((it && it.build_run_url) || '').trim());
const hasTicket = (it) => !!String((it && it.jira_ticket) || '').trim();

/** 'DF' for a Dataflow image (item.df_only), else 'CARE'. */
export function itemKind(it) {
    return it && it.df_only ? 'DF' : 'CARE';
}

/**
 * A history row's state:
 *  'in-queue'    — it.in_queue (already queued for the next release; cannot be ticked)
 *  'ready'       — it.from_queue (qualified once, goes straight back), OR a never-queued row that has BOTH
 *                  a build_run_url matching /^https?:\/\/\S+\/actions\/runs\/\d+/ AND a non-empty jira_ticket
 *  'needs-input' — never queued and missing the run and/or the ticket (the gate needs both)
 * @returns {'in-queue'|'ready'|'needs-input'}
 */
export function itemState(it) {
    if (it && it.in_queue) return 'in-queue';
    if (it && it.from_queue) return 'ready';
    return hasRun(it) && hasTicket(it) ? 'ready' : 'needs-input';
}

/**
 * Filter the releases for display. query: case-insensitive substring over artifact_name, artifact_version and
 * jira_ticket (trimmed; '' matches everything). kind: 'all' | 'CARE' | 'DF'. Returns an array of
 * { rel, index, items: [{ it, index }] } where index/items[].index are the ORIGINAL positions (so ticks keyed
 * "releaseIndex:itemIndex" stay stable across filtering). A release with no matching items is dropped.
 * Order preserved (the API already returns newest first).
 */
export function filterReleases(releases, { query = '', kind = 'all' } = {}) {
    const q = String(query || '').trim().toLowerCase();
    const byKind = kind === 'CARE' || kind === 'DF';
    const matches = (it) => {
        if (byKind && itemKind(it) !== kind) return false;
        if (!q) return true;
        return [it.artifact_name, it.artifact_version, it.jira_ticket]
            .some(v => String(v || '').toLowerCase().includes(q));
    };
    const shown = [];
    (releases || []).forEach((rel, index) => {
        const items = [];
        ((rel && rel.items) || []).forEach((it, i) => { if (it && matches(it)) items.push({ it, index: i }); });
        if (items.length) shown.push({ rel, index, items });
    });
    return shown;
}

/**
 * Item indices "Re-queue all" ticks for one release: every item whose itemState is 'ready'.
 * `onlyIndices` (the rows a search or filter leaves visible) narrows it: a person must be able to see
 * — and untick — everything the button ticks, so a filtered view never ticks rows it is hiding.
 */
export function requeueAllIndices(rel, onlyIndices) {
    const only = onlyIndices ? new Set(onlyIndices) : null;
    const picked = [];
    ((rel && rel.items) || []).forEach((it, i) => {
        if (itemState(it) === 'ready' && (!only || only.has(i))) picked.push(i);
    });
    return picked;
}

/**
 * What a needs-input row still lacks for the gate: [], ['run'], ['ticket'] or ['run', 'ticket'].
 * In-queue and qualified rows lack nothing (they never take the gate from here).
 */
export function missingInputs(it) {
    if (!it || it.in_queue || it.from_queue) return [];
    return [hasRun(it) ? '' : 'run', hasTicket(it) ? '' : 'ticket'].filter(Boolean);
}

/**
 * The row as the gate will see it: the record with anything the person typed over it — the run
 * trimmed, the ticket trimmed and upper-cased (JIRA keys are). A field left untyped keeps the record's value.
 * @param {object} it @param {{run?: string, jira?: string}} [typed]
 */
export function withTyped(it, typed) {
    const merged = { ...(it || {}) };
    if (typed && typed.run != null) merged.build_run_url = String(typed.run).trim();
    if (typed && typed.jira != null) merged.jira_ticket = String(typed.jira).trim().toUpperCase();
    return merged;
}

/** The run URL as a link target, or '' — only http(s) ever becomes a live link (never javascript: and friends). */
export function safeRunHref(url) {
    const s = String(url || '').trim(), l = s.toLowerCase();
    return l.startsWith('https://') || l.startsWith('http://') ? s : '';
}

/**
 * The short summary shown on a (collapsed) release header, e.g. "3 charts · 2 ready · 1 in next release"
 * or "1 chart · needs run + ticket". Counts by state; omit zero counts; singular/plural correct.
 * When one state covers every chart its count repeats the chart count, so it
 * reads "ready" (one chart) or "all ready" (several) instead.
 */
export function releaseSummary(rel) {
    const items = (rel && rel.items) || [];
    const total = items.length;
    const parts = [total + ' chart' + (total === 1 ? '' : 's')];
    const count = { 'ready': 0, 'in-queue': 0, 'needs-input': 0 };
    let missRun = false, missTicket = false;
    for (const it of items) {
        const state = itemState(it);
        count[state] += 1;
        if (state === 'needs-input') {
            if (!hasRun(it)) missRun = true;
            if (!hasTicket(it)) missTicket = true;
        }
    }
    const lead = (n) => (n === total ? (n === 1 ? '' : 'all ') : n + ' ');
    if (count['ready']) parts.push(lead(count['ready']) + 'ready');
    if (count['in-queue']) parts.push(lead(count['in-queue']) + 'in next release');
    const n = count['needs-input'];
    if (n) {
        const what = missRun && missTicket ? 'run + ticket' : (missRun ? 'run' : 'ticket');
        parts.push(lead(n) + (n === 1 ? 'needs ' : 'need ') + what);
    }
    return parts.join(' · ');
}
