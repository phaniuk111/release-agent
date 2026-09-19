// Wording for the BQ cost report pill — shared with the Backstage port, so pure
// and tested in tests/js/bq_cost.test.mjs. The server measures (tools/bq_cost.py:
// INFORMATION_SCHEMA reads and dry runs, never a row of data); this only says
// what it found. Every function takes a payload with fields missing and still
// answers — a half-built report is shown, not a blank card.
import { shortName } from './format.js';

const K = 1024;   // bytes per KB, MB per GB, GB per TB, TB per PB

/** A finite number, or null for anything that is not one (null, '', 'n/a'). */
function num(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
}

/** An array, or [] for anything else — a payload's list is never assumed. */
function list(v) {
    return Array.isArray(v) ? v : [];
}

/** Three figures are enough for a size: 12.6, 84.2, 380. */
function trim(value) {
    const abs = Math.abs(value);
    if (abs >= 100) return formatCount(value);
    if (abs >= 0.1) return value.toFixed(1);
    return value.toFixed(2);
}

function plural(n, one, many) {
    return num(n) === 1 ? one : many;
}

/** 1240 → "1,240". Hand-rolled so the text is the same in every locale. */
export function formatCount(n) {
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
export function formatGb(gb) {
    const n = num(gb);
    if (n === null) return '—';
    if (n === 0) return '0 GB';
    const abs = Math.abs(n);
    if (abs >= K * K) return trim(n / (K * K)) + ' PB';
    if (abs >= K) return trim(n / K) + ' TB';
    if (abs < 0.1) return trim(n * K) + ' MB';
    return trim(n) + ' GB';
}

/** A byte count in the unit that reads best — the server's GB figure rounds a
 * 21 KB query to 0.0, so screens prefer this whenever raw bytes are present:
 * 21557 → "21.1 KB", 13200000 → "12.6 MB", 0 → "0 B". */
export function formatBytes(bytes) {
    const n = num(bytes);
    if (n === null) return '—';
    const abs = Math.abs(n);
    if (abs < K) return formatCount(n) + ' B';
    if (abs < K * K) return trim(n / K) + ' KB';
    return formatGb(n / (K * K * K));
}

/** "$0.01", "$112.50", "$1,234.50"; null → "—". `decimals` 0 for a monthly figure. */
export function formatUsd(usd, decimals = 2) {
    const n = num(usd);
    if (n === null) return '—';
    const fixed = Math.abs(n).toFixed(decimals);
    const dot = fixed.indexOf('.');
    const whole = dot === -1 ? fixed : fixed.slice(0, dot);
    const frac = dot === -1 ? '' : fixed.slice(dot);
    return (n < 0 ? '-' : '') + '$' + formatCount(whole) + frac;
}

/** The line under the heading: "1,240 queries · 612 slot-hours · 84.2 GB billed ·
 * this report read 0.4 GB". Parts the scan did not measure are left out;
 * a disabled or failed report says so instead of showing zeros — the failure's
 * own text belongs in the card body, not repeated in the heading. */
export function costSummary(report) {
    if (!report || typeof report !== 'object') return 'no report';
    if (report.disabled) return 'BigQuery cost report is disabled';
    if (report.ok === false) return 'BigQuery cost report is unavailable';
    const t = report.totals && typeof report.totals === 'object' ? report.totals : {};
    const parts = [];
    if (num(t.queries) !== null) parts.push(formatCount(t.queries) + plural(t.queries, ' query', ' queries'));
    if (num(t.slot_hours) !== null) parts.push(trim(num(t.slot_hours)) + ' slot-hours');
    if (num(t.gb_billed) !== null) parts.push(formatGb(t.gb_billed) + ' billed');
    if (num(t.cache_hit_pct) !== null) parts.push(trim(num(t.cache_hit_pct)) + '% from cache');
    if (num(report.report_cost_bytes) !== null) {
        parts.push('this report read ' + formatBytes(num(report.report_cost_bytes)));
    }
    return parts.join(' · ');
}

/** "my-project · region-us · last 14 days · on-demand billing · scanned …". */
export function reportMeta(report) {
    const r = report && typeof report === 'object' ? report : {};
    const parts = [];
    if (r.project) parts.push(String(r.project));
    if (r.region) parts.push(String(r.region));
    if (num(r.days) !== null) parts.push('last ' + formatCount(r.days) + plural(r.days, ' day', ' days'));
    if (r.billing) parts.push(String(r.billing) + ' billing');
    if (r.scanned_at) parts.push('scanned ' + String(r.scanned_at));
    return parts.join(' · ');
}

/** What the table does not show but the Excel does: "storage: 3 findings ·
 * writes: unavailable · history: 2 adopted, 1 still open — in the Excel".
 * "" when there is nothing beyond the table. */
export function extrasText(report) {
    const r = report && typeof report === 'object' ? report : {};
    const parts = [];
    const section = (name, items, error) => {
        const n = list(items).length;
        if (n) parts.push(name + ': ' + formatCount(n) + plural(n, ' finding', ' findings'));
        else if (error) parts.push(name + ': unavailable');
    };
    section('storage', r.storage, r.storage_error);
    section('writes', r.writes, r.writes_error);
    const h = historyText(r.history);
    if (h) parts.push('history: ' + h.split(' · ').join(', '));
    return parts.length ? parts.join(' · ') + ' — in the Excel' : '';
}

/** A window with nothing worth a row. */
export function emptyText(report) {
    const d = num(report && report.days);
    return d === null ? 'nothing significant in the window'
        : 'nothing significant in the last ' + formatCount(d) + plural(d, ' day', ' days');
}

/** How to turn the report on — the config names, since that is what someone
 * has to set (helm values.yaml → config:). The roles are design §2. */
export const ENABLE_HINT = 'To enable it, set BQ_COST_REGION to the INFORMATION_SCHEMA region ' +
    '(e.g. region-europe-west3) and BQ_COST_PROJECT (or BQ_PROJECT) to the project to scan; ' +
    'the service account needs bigquery.resourceViewer, metadataViewer and jobUser there — ' +
    'never dataViewer.';

/** Biggest first: slot-hours on reservations (what the team pays for), bytes
 * billed on-demand. Stable, so equal shapes keep the server's order. */
export function orderShapes(shapes, billing) {
    const key = billing === 'reservations' ? 'slot_hours' : 'gb_billed';
    const of = (s) => num(s && s[key]) || 0;
    return list(shapes).map((s, i) => [s, i])
        .sort((a, b) => (of(b[0]) - of(a[0])) || (a[1] - b[1]))
        .map(([s]) => s);
}

/** Who runs a shape, in the tight column: "alice", or "alice +2" when others do
 * too. `users` is a sample of the emails (the server caps it); `users_count`
 * is the real distinct count, so it decides the "+N" when present. */
export function whoText(shape) {
    const s = shape && typeof shape === 'object' ? shape : {};
    const who = String(s.who || list(s.users)[0] || '');
    const name = shortName(who);
    const count = num(s.users_count);
    const others = count !== null ? Math.max(0, count - 1) : list(s.users).filter(u => u && u !== who).length;
    return name + (others ? ' +' + others : '');
}

/** BigQuery's performance insights in words: ["slot_contention"] → "slot contention",
 * joined with ", "; nothing → "". Repeats (one per stage) are said once. */
export function insightText(insights) {
    const seen = new Set();
    const words = [];
    list(insights).forEach(i => {
        const raw = i && typeof i === 'object' ? (i.name || i.kind || i.type || '') : i;
        const w = String(raw == null ? '' : raw).split('_').join(' ').trim();
        if (w && !seen.has(w)) { seen.add(w); words.push(w); }
    });
    return words.join(', ');
}

/** "3 adopted · 2 still open" from the memory across runs (design §7) — the
 * counts, given either as numbers or as an adoption list to count; "" without. */
export function historyText(history) {
    if (!history || typeof history !== 'object') return '';
    const counts = { adopted: 0, still_open: 0, new: 0, gone: 0 };
    list(history.adoption).forEach(a => {
        if (a && Object.prototype.hasOwnProperty.call(counts, a.status)) counts[a.status] += 1;
    });
    Object.keys(counts).forEach(k => { if (num(history[k]) !== null) counts[k] = num(history[k]); });
    const words = { adopted: 'adopted', still_open: 'still open', new: 'new', gone: 'gone' };
    return Object.keys(counts).filter(k => counts[k] > 0).map(k => formatCount(counts[k]) + ' ' + words[k]).join(' · ');
}

/** What the chat is asked when someone clicks "Ask why" on a row — the model
 * does the investigating (bq_query_detail, the tables) and proves any rewrite
 * with bq_verify_rewrite; this only names the shape and its cost. */
export function explainPrompt(shape) {
    const s = shape && typeof shape === 'object' ? shape : {};
    const facts = [];
    if (num(s.runs) !== null) facts.push(formatCount(s.runs) + plural(s.runs, ' run', ' runs'));
    if (num(s.gb_billed) !== null) facts.push(formatGb(s.gb_billed) + ' billed');
    return 'Why is BigQuery query shape ' + String(s.qhash || '?') + ' expensive' +
        (facts.length ? ' (' + facts.join(', ') + ')' : '') +
        '? Use bq_query_detail, look at the referenced tables, and propose a cheaper rewrite ' +
        'tested with bq_verify_rewrite.';
}
