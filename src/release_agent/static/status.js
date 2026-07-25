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

export function toggleBannerDetail() {
    const d = document.getElementById('rb-detail');
    if (d) d.classList.toggle('hidden');
}

export async function loadReleaseStatus() {
    const banner = document.getElementById('release-banner');
    const dot    = document.getElementById('rb-dot');
    const title  = document.getElementById('rb-title');
    const detail = document.getElementById('rb-detail');
    try {
        const res = await fetch(API_BASE + '/api/release-status');
        const s = await res.json();
        banner.classList.remove('hidden');
        if (s.error) {
            dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
            title.textContent = "Couldn't fetch release status";
            detail.textContent = s.error;
            return;
        }
        const foot = 'now ' + s.now_utc + ' UTC · ' + s.date_utc;
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
        const pr = s.prd_release_pr;
        if (pr) {
            const n = (s.pending_to_prod || []).length;
            dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
            title.textContent = 'Release PR #' + pr.number + ' open · ' + n + ' change' + (n === 1 ? '' : 's') + ' staged' + blkTitle + qTitle;
            let html = (s.reason ? esc(s.reason) + ' · ' : '') + foot;
            if ((pr.charts || []).length) {
                html += '<br><span>staged: ' + _chartList(pr.charts) + '</span>';
            }
            html += ' &nbsp;<a href="' + esc(pr.url) + '" target="_blank" class="underline text-emerald-400">open PR #' + esc(pr.number) + '</a>';
            html += _envLists(s);
            html += blkDetail;
            detail.innerHTML = html;
            return;
        }
        dot.className = blk
            ? 'w-2 h-2 rounded-full bg-amber-400 inline-block'
            : 'w-2 h-2 rounded-full bg-emerald-500 inline-block';
        title.textContent = 'No release open · PRD: ' + (s.prd_charts || []).length + ' charts' + blkTitle + qTitle;
        detail.innerHTML = (s.reason ? esc(s.reason) + ' · ' : '') + foot + _envLists(s) + blkDetail;
    } catch (e) {
        banner.classList.remove('hidden');
        dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
        title.textContent = "Couldn't reach the release-status endpoint";
        detail.textContent = String(e);
    }
}
