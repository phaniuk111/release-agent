// One-line release status strip — reads /api/release-status:
// { date_utc, now_utc, uat_charts, prd_charts, queued_next, reason,
//   blocking_pr: {number,url,head,base,tickets}, df: {...} }
// There is no staged-PRD-batch slot any more: PROD is reached only by promoting
// a release, so the PR open against a guarded branch (blocking_pr) IS the CARE
// release in flight — the same reading the DF slot has always used. Every field
// is optional; the strip must render whatever is missing.
import { getAutoTriage, releaseStatus } from './api.js';
import { stripLine } from './core/auto_triage.js';
import { hasFeature } from './core/capabilities.js';
import { escapeHtml as esc } from './core/format.js';
import { releaseLine, releasePrs, releaseTitle, ticketParts } from './core/tickets.js';
import { showAutoTriage } from './forms/auto_triage.js';

function _chartList(arr) {
    return (arr || []).map(function(c){ return esc(c.helm_chart_name) + ':' + esc(c.helm_chart_version); })
        .join(' &nbsp;│&nbsp; ');
}

// The detail behind the banner's "PRD: N charts" summary, so a click answers
// "which N?". PRD only — the banner is about the release state; UAT contents
// live in the Insights panel's deployed-state view.
function _envLists(s) {
    if (!(s.prd_charts || []).length) return '';
    return '<br><span class="text-slate-300">PRD (' + s.prd_charts.length + '):</span> ' + _chartList(s.prd_charts);
}

// The CHG / RMG tickets named on a release PR, after its number: each a link to
// where it was named; a prefix the PR does not name yet reads "no CHG yet" in
// grey. Never a state — the comments do not say one. Nothing when the server sent no tickets (no PR, or the
// PR could not be read) — core/tickets.js decides.
function _ticketsHtml(pr) {
    const parts = ticketParts(pr && pr.tickets);
    if (!parts.length) return '';
    return ' · ' + parts.map(function (p) {
        if (p.missing) return '<span class="text-slate-500">' + esc(p.text) + '</span>';
        return p.url
            ? '<a href="' + esc(p.url) + '" target="_blank" class="underline">' + esc(p.id) + '</a>'
            : esc(p.id);
    }).join(' · ');
}

let _lastLoadedAt = 0;

// The banner is fetched ONLY on demand: reading it costs 5 GitHub API calls and
// the answer is the same for everyone, so auto-loading it per page view (and
// the old 60s poll) burned the shared PAT's rate limit for data nobody asked
// for. Show the strip in an idle state until the user clicks ⟳.
export function showBannerIdle() {
    const banner = document.getElementById('release-banner');
    const dot = document.getElementById('rb-dot');
    const title = document.getElementById('rb-title');
    if (!banner) return;
    banner.classList.remove('hidden');
    dot.className = 'w-2 h-2 rounded-full bg-slate-600 inline-block';
    title.textContent = 'Release status — click ⟳ to load';
}

// Age the banner label in place ("· 3m ago") without re-fetching anything.
export function startBannerAgeTicker() {
    setInterval(function () {
        const el = document.getElementById('rb-age');
        if (!el || !_lastLoadedAt) return;
        const mins = Math.floor((Date.now() - _lastLoadedAt) / 60000);
        el.textContent = mins >= 1 ? ' · ' + mins + 'm ago' : '';
    }, 30000);
}

export function toggleBannerDetail() {
    const d = document.getElementById('rb-detail');
    if (d) d.classList.toggle('hidden');
}

// CARE and DF are separate releases in separate repos. The banner leads with
// CARE (the weekly cut most people mean) and appends DF only when it is
// configured separately — an unconfigured or unreachable DF must never make the
// CARE status look wrong.
function _dfSummary(s) {
    const df = s.df;
    if (!df) return { title: '', detail: '' };
    if (df.error) {
        return {
            title: ' · DF: unavailable',
            detail: '<br><span class="text-amber-400">DF release status unavailable (' +
                esc(df.error) + ')</span>',
        };
    }
    // A PR open on the DF chain IS the DF release in flight; the server may name
    // that slot either way, so read both rather than blanking the DF summary.
    const pr = df.release_pr || df.prd_release_pr || df.blocking_pr;
    if (pr) {
        return {
            title: ' · DF: PR #' + pr.number + ' open',
            detail: '<br><span class="text-sky-300">DF release:</span> ' +
                '<a href="' + esc(pr.url) + '" target="_blank" class="underline text-sky-400">PR #' +
                esc(pr.number) + '</a>' + _ticketsHtml(pr) +
                ((pr.charts || []).length ? ' — staged: ' + _chartList(pr.charts) : ''),
        };
    }
    return {
        title: ' · DF: none open',
        // No image count here either — same reason as the CARE title.
        detail: '<br><span class="text-sky-300">DF release:</span> none open',
    };
}

// ---- Auto-triage strip ---------------------------------------------------------
// One line under Release status: "Auto-triage · every 10 min · last check
// 09:41 · 2 needs attention · 3 open · next 09:51" — amber when something
// needs attention, red when the last pass failed. Unlike the banner it costs
// no GitHub call (one read of the event log), so it refreshes itself every
// minute while the tab is visible and at once on focus. Absent entirely when
// the auto-triage feature is not on for this person. Built here, not in the
// HTML shell: the shell does not need to know about a preview feature.
const STRIP_MS = 60000;
const STRIP_DOT = { ok: 'bg-emerald-500', warn: 'bg-amber-400', error: 'bg-red-600', off: 'bg-slate-600' };

async function _refreshAutoTriageStrip() {
    const dot = document.getElementById('at-dot');
    const title = document.getElementById('at-title');
    if (!dot || !title) return;
    let feed;
    try { feed = await getAutoTriage(''); } catch (e) { feed = { ok: false, error: String((e && e.message) || e), status: {} }; }
    const line = stripLine(feed);
    dot.className = 'w-2 h-2 rounded-full inline-block ' + (STRIP_DOT[line.tone] || STRIP_DOT.off);
    title.textContent = line.text;
}

export function startAutoTriageStrip() {
    if (!hasFeature(window.PORTAL_UI, 'auto-triage')) return;
    const banner = document.getElementById('release-banner');
    if (!banner || document.getElementById('auto-triage-strip')) return;
    const strip = document.createElement('div');
    strip.id = 'auto-triage-strip';
    strip.className = 'mb-3 rounded-xl border px-3 py-1.5 text-xs border-slate-800 bg-slate-900/60';
    strip.innerHTML =
        '<div class="flex items-center gap-2">' +
        '<span id="at-dot" class="w-2 h-2 rounded-full bg-slate-600 inline-block"></span>' +
        '<span id="at-title" class="text-slate-300 truncate cursor-pointer hover:text-white" ' +
        'title="Open the Auto-triage card">Auto-triage — checking…</span>' +
        '<span class="flex-1"></span>' +
        '<button id="at-refresh" title="Refresh now" class="text-slate-600 hover:text-slate-400">' +
        '<i class="fa-solid fa-rotate-right"></i></button></div>';
    banner.insertAdjacentElement('afterend', strip);
    strip.querySelector('#at-title').addEventListener('click', () => showAutoTriage());
    strip.querySelector('#at-refresh').addEventListener('click', _refreshAutoTriageStrip);
    _refreshAutoTriageStrip();
    setInterval(() => { if (document.visibilityState === 'visible') _refreshAutoTriageStrip(); }, STRIP_MS);
    window.addEventListener('focus', _refreshAutoTriageStrip);
}

export async function loadReleaseStatus(fresh) {
    const banner = document.getElementById('release-banner');
    const dot    = document.getElementById('rb-dot');
    const title  = document.getElementById('rb-title');
    const detail = document.getElementById('rb-detail');
    try {
        const s = await releaseStatus(fresh);
        banner.classList.remove('hidden');
        if (s.error) {
            dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
            title.textContent = "Couldn't fetch release status";
            detail.textContent = s.error;
            return;
        }
        // The banner is a shared snapshot, not a live feed — say how old it is
        // so "⟳" is an obvious, meaningful action rather than decoration.
        _lastLoadedAt = Date.now();
        const foot = 'as of ' + s.now_utc + ' UTC · ' + s.date_utc;
        // A PR into a release-guard branch (PRD/PRL1/...) blocks adds — surface it
        // in the strip so users learn BEFORE filling a deploy form.
        // Every PR open on a guarded branch (UAT→PRD and UAT→PRL1 can both be in
        // flight, each with its own CHG); one line each in the details. An open
        // PR is BOTH the release in flight and the reason adds are blocked, so
        // the title names each number once.
        // One line per CARE release in flight — UAT→PRL1 and UAT→PRD each with
        // its own CHG and RMG ("CARE PRL1 · CHG… · RMG…"). Adds are blocked only
        // while the release guard holds (blocking_pr), not by every listed PR.
        const prs = releasePrs(s);
        const blk = s.blocking_pr && s.blocking_pr.number ? s.blocking_pr : null;
        const careTitle = releaseTitle(prs, !!blk);
        // Intake-queue awareness: how many charts devs have registered for the
        // NEXT release (absent when the BQ queue is disabled).
        const qTitle = (typeof s.queued_next === 'number' && s.queued_next > 0)
            ? ' · ' + s.queued_next + ' queued for next release' : '';
        const blkDetail = prs.map(p => {
                const line = releaseLine(p);
                return '<br><span class="text-slate-200 font-medium">' + esc(line.label) + '</span>' + _ticketsHtml(p) +
                    ' <span class="text-slate-500">— <a href="' + esc(line.url) + '" target="_blank" class="underline">PR #' +
                    esc(line.number) + '</a> (' + esc(line.head) + ' → ' + esc(line.base) + ')</span>';
            }).join('') +
            (blk ? '<br><span class="text-amber-400">⚠ Adds to the release are blocked until PR #' + esc(blk.number) +
                   ' is merged or closed.</span>' : '');
        const df = _dfSummary(s);
        dot.className = blk
            ? 'w-2 h-2 rounded-full bg-amber-400 inline-block'
            : 'w-2 h-2 rounded-full bg-emerald-500 inline-block';
        // No installed-chart count in the title: it is the size of the deployed
        // SET, which barely moves release to release, so it read as activity while
        // reporting none. The full PRD list is still one click away under details.
        title.textContent = careTitle + df.title + qTitle;
        detail.innerHTML = (s.reason ? esc(s.reason) + ' · ' : '') + foot + _envLists(s) + df.detail + blkDetail;
    } catch (e) {
        banner.classList.remove('hidden');
        dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
        title.textContent = "Couldn't reach the release-status endpoint";
        detail.textContent = String(e);
    }
}
