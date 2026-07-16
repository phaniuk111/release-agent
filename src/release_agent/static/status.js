// One-line release status strip — reads the PRD-release-PR API shape:
// { date_utc, now_utc, uat_charts, prd_charts,
//   prd_release_pr: {number,url,charts,can_merge_now}, pending_to_prod, reason }
import { API_BASE } from './state.js';

function _chartList(arr) {
    return (arr || []).map(function(c){ return c.helm_chart_name + ':' + c.helm_chart_version; })
        .join(' &nbsp;│&nbsp; ');
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
        const pr = s.prd_release_pr;
        if (pr) {
            const n = (s.pending_to_prod || []).length;
            dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
            title.textContent = 'Release PR #' + pr.number + ' open · ' + n + ' change' + (n === 1 ? '' : 's') + ' staged';
            let html = (s.reason ? s.reason + ' · ' : '') + foot;
            if ((pr.charts || []).length) {
                html += '<br><span>staged: ' + _chartList(pr.charts) + '</span>';
            }
            html += ' &nbsp;<a href="' + pr.url + '" target="_blank" class="underline text-emerald-400">open PR #' + pr.number + '</a>';
            detail.innerHTML = html;
            return;
        }
        dot.className = 'w-2 h-2 rounded-full bg-emerald-500 inline-block';
        title.textContent = 'No release open · PRD: ' + (s.prd_charts || []).length + ' charts';
        detail.innerHTML = (s.reason ? s.reason + ' · ' : '') + foot;
    } catch (e) {
        banner.classList.remove('hidden');
        dot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';
        title.textContent = "Couldn't reach the release-status endpoint";
        detail.textContent = String(e);
    }
}
