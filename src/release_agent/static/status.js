// One-line release status strip — reads the PRD-release-PR API shape:
// { date_utc, now_utc, uat_charts, prd_charts,
//   prd_release_pr: {number,url,charts,can_merge_now}, pending_to_prod, reason }
import { API_BASE } from './state.js';
import { escapeHtml as esc } from './chat.js';

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
    const pr = df.prd_release_pr;
    if (pr) {
        return {
            title: ' · DF: PR #' + pr.number + ' open',
            detail: '<br><span class="text-sky-300">DF release:</span> ' +
                '<a href="' + esc(pr.url) + '" target="_blank" class="underline text-sky-400">PR #' +
                esc(pr.number) + '</a>' +
                ((pr.charts || []).length ? ' — staged: ' + _chartList(pr.charts) : ''),
        };
    }
    return {
        title: ' · DF: none open',
        // No image count here either — same reason as the CARE title.
        detail: '<br><span class="text-sky-300">DF release:</span> none open',
    };
}

export async function loadReleaseStatus(fresh) {
    const banner = document.getElementById('release-banner');
    const dot    = document.getElementById('rb-dot');
    const title  = document.getElementById('rb-title');
    const detail = document.getElementById('rb-detail');
    try {
        const res = await fetch(API_BASE + '/api/release-status' + (fresh ? '?fresh=1' : ''));
        const s = await res.json();
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
        const blk = s.blocking_pr;
        const blkTitle = blk ? ' · ⚠ adds blocked by PR #' + blk.number : '';
        // Intake-queue awareness: how many charts devs have registered for the
        // NEXT release (absent when the BQ queue is disabled).
        const qTitle = (typeof s.queued_next === 'number' && s.queued_next > 0)
            ? ' · ' + s.queued_next + ' queued for next release' : '';
        const blkDetail = blk
            ? '<br><span class="text-amber-400">⚠ Adds to the release are blocked: ' +
              '<a href="' + esc(blk.url) + '" target="_blank" class="underline">PR #' + esc(blk.number) +
              '</a> (' + esc(blk.head) + ' → ' + esc(blk.base) + ') is open — merge or close it first.</span>'
            : '';
        const df = _dfSummary(s);
        const pr = s.prd_release_pr;
        if (pr) {
            const n = (s.pending_to_prod || []).length;
            dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
            title.textContent = 'CARE: PR #' + pr.number + ' open · ' + n + ' change' + (n === 1 ? '' : 's') + ' staged' + df.title + blkTitle + qTitle;
            let html = (s.reason ? esc(s.reason) + ' · ' : '') + foot;
            if ((pr.charts || []).length) {
                html += '<br><span>staged: ' + _chartList(pr.charts) + '</span>';
            }
            html += ' &nbsp;<a href="' + esc(pr.url) + '" target="_blank" class="underline text-emerald-400">open PR #' + esc(pr.number) + '</a>';
            html += _envLists(s);
            html += df.detail;
            html += blkDetail;
            detail.innerHTML = html;
            return;
        }
        dot.className = blk
            ? 'w-2 h-2 rounded-full bg-amber-400 inline-block'
            : 'w-2 h-2 rounded-full bg-emerald-500 inline-block';
        // No installed-chart count in the title: it is the size of the deployed
        // SET, which barely moves release to release, so it read as activity while
        // reporting none. The full PRD list is still one click away under details.
        title.textContent = 'CARE: no release open' + df.title + blkTitle + qTitle;
        detail.innerHTML = (s.reason ? esc(s.reason) + ' · ' : '') + foot + _envLists(s) + df.detail + blkDetail;
    } catch (e) {
        banner.classList.remove('hidden');
        dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
        title.textContent = "Couldn't reach the release-status endpoint";
        detail.textContent = String(e);
    }
}
