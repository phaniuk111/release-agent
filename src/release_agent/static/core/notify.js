// The notification bell's rules — what the dropdown shows and where a click
// goes. Pure: no DOM, no fetch, no globals, so the Node tests import it and
// the Backstage port's bell can use it unchanged. The items are what
// GET /api/notifications returns (tools/notifications.py `list_for`).

export const POLL_MS = 60000;
export const DEFAULT_DAYS = 14;

// Only the tab a person is looking at polls: a bell nobody can see costs a
// BigQuery read a minute per open tab, for a badge nobody sees change.
export function shouldPoll(visibilityState) {
    return visibilityState === 'visible';
}

// A dismissed item is gone from the bell; the rest is shown unread first, each
// part newest first — `at` is when the text last changed, so an item that was
// updated (a PR merged, then its runs finished) rises with its new text.
export function groupItems(items) {
    const kept = (Array.isArray(items) ? items : []).filter(i => i && i.status !== 'dismissed');
    const byNewest = (a, b) => String(b.at || '').localeCompare(String(a.at || ''));
    return {
        unread: kept.filter(i => i.status === 'unread').sort(byNewest),
        read: kept.filter(i => i.status !== 'unread').sort(byNewest),
    };
}

// The dot beside an item: the colour says how serious, filled says unread.
const COLOURS = { info: '#34d399', warning: '#fbbf24', critical: '#f87171' };

export function dotStyle(item) {
    const colour = COLOURS[item && item.severity] || COLOURS.info;
    const filled = !!item && item.status === 'unread';
    return {
        filled,
        colour,
        // Inline, because the vendored Tailwind only carries classes already
        // in use — a hollow coloured ring is not one of them.
        style: filled
            ? 'background:' + colour + ';border:2px solid ' + colour
            : 'background:transparent;border:2px solid ' + colour,
    };
}

// The badge on the bell: nothing at zero, capped so a backlog does not widen
// the header.
export function badgeText(unread) {
    const n = Number(unread) || 0;
    if (n <= 0) return '';
    return n > 9 ? '9+' : String(n);
}

// How old an item is, in the shortest words that still read: "now", "2m",
// "1h", "yesterday", "3d". `now` is injectable for tests.
export function age(iso, now = Date.now()) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, (now - t) / 1000);
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    if (s < 2 * 86400) return 'yesterday';
    return Math.floor(s / 86400) + 'd';
}

// The footer's "Checked 5s ago": when the list was last fetched, so a stale
// dropdown (the tab was hidden, the poll paused) says so.
export function checkedText(checkedAtMs, now = Date.now()) {
    if (!checkedAtMs) return 'Not checked yet';
    const s = Math.max(0, Math.floor((now - checkedAtMs) / 1000));
    if (s < 5) return 'Checked just now';
    if (s < 60) return 'Checked ' + s + 's ago';
    if (s < 3600) return 'Checked ' + Math.floor(s / 60) + 'm ago';
    return 'Checked ' + Math.floor(s / 3600) + 'h ago';
}

// The one line of body under the title: its first line, cut so a long tool
// note does not turn a row into a paragraph. The card shows the whole body.
export function bodyLine(body, max = 110) {
    const first = String(body == null ? '' : body).split('\n')[0].trim();
    if (first.length <= max) return first;
    return first.slice(0, max - 1).trimEnd() + '…';
}

// Where a click goes. A notification's `link` is one of: a `portal:` action
// the page performs, `chat:<message>` to put in the chat, or a URL for a new
// tab. Only http(s) opens a tab — a stored link is data, and `javascript:`
// must never run from it. Everything unknown is a no-op, not an error.
export function routeLink(link) {
    const raw = String(link == null ? '' : link).trim();
    if (!raw) return { kind: 'none' };
    if (raw === 'portal:queue') return { kind: 'queue' };
    if (raw.startsWith('portal:support:')) {
        // portal:support:<incident_id>:<date> (an Auto-triage finding; the stored
        // link keeps its name) — the id may itself carry ':'
        // (a signature hash does not, but nothing guarantees it), so the date
        // is the LAST segment and the id everything between.
        const rest = raw.slice('portal:support:'.length);
        const cut = rest.lastIndexOf(':');
        const incidentId = cut < 0 ? rest : rest.slice(0, cut);
        const date = cut < 0 ? '' : rest.slice(cut + 1);
        return { kind: 'auto-triage', incidentId, date };
    }
    if (raw.startsWith('portal:deploy:')) {
        const what = raw.slice('portal:deploy:'.length).trim();
        return what
            ? { kind: 'chat', message: 'What is the state of the deploy ' + what + '?' }
            : { kind: 'none' };
    }
    if (raw.startsWith('chat:')) {
        const message = raw.slice('chat:'.length).trim();
        return message ? { kind: 'chat', message } : { kind: 'none' };
    }
    if (raw.startsWith('https://') || raw.startsWith('http://')) return { kind: 'url', url: raw };
    return { kind: 'none' };
}

// What the bell's empty state says — per the answer, not one sentence for
// every reason the list is empty.
export function emptyText(res) {
    if (!res || res.ok === false) return (res && res.error) || 'Notifications could not be read.';
    if (res.needsEmail) return 'Enter your email to see your notifications.';
    return 'Nothing new in the last ' + DEFAULT_DAYS + ' days.';
}

// One sentence per kind for the card's "What" column — the kinds come from
// tools/notifications.py; an unknown one shows as itself.
const KIND_LABELS = {
    release_pr: 'Release PR', uat_pr: 'UAT PR', df_run: 'DF run', requeue: 'Queue',
    chg: 'CHG', deploy_started: 'Deploy', deploy_finished: 'Deploy', auto_triage: 'Auto-triage',
};

export function kindLabel(kind) {
    return KIND_LABELS[kind] || String(kind || '');
}
