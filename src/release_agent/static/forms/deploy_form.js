import { deployTemplatePath, getContext } from '../api.js';
import { sendMessage } from '../chat.js';
import { ctxNote, opening, withDismiss } from './common.js';
import { showDfDeployForm } from './df_deploy_form.js';
import { parseDeployInclude } from './parse.js';
import { showQueueForm } from './queue_form.js';
import { showQueueTable } from './queue_table.js';
import { showReleaseForm } from './release_form.js';

// Deploy editor — shows the ACTUAL current deployment.json as editable JSON
// (pre-filled from /api/deploy-template, which reads the live uat/ or prd file;
// a chart named from a chat command is upserted in). On submit it sends
// {environment, include} through /api/chat; the backend previews the exact JSON
// it will write and asks to confirm.
export async function showDeployForm(env, name, version) {
    if (env === 'df-uat') { showDfDeployForm(); return; }
    if (env === 'release') { showReleaseForm(); return; }
    if (env === 'df-release') { showReleaseForm('df'); return; }
    if (env === 'queue') { showQueueForm(); return; }
    if (env === 'queue-table') { showQueueTable(); return; }
    const isProd = env === 'prod';
    const accentT = isProd ? 'text-amber-300' : 'text-emerald-300';
    const accentBtn = isProd ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500';
    const icon = isProd ? 'fa-shield-halved' : 'fa-flask';
    // "CARE UAT" distinguishes the helm-chart lane from the Dataflow one, which
    // deploys to the same environment by a different mechanism.
    const heading = isProd ? 'Deploy to PROD' : 'Deploy to CARE UAT';

    // Pre-fill the editor with the WHOLE current deployment.json ({"include":[...]})
    // from the backend (the live uat/ or prd file) — edit entries, add more to deploy
    // several charts at once. The fallback below is only used if the fetch fails.
    const _ph = opening(heading);
    const dctx = await getContext(deployTemplatePath({ env: env, name: name || '', version: version || '' }),
                                  { deployment: null, deploy_repo: '' });
    _ph.remove();
    const fileDoc = dctx.deployment
        || { include: [ { helm_chart_name: name || '', helm_chart_version: version || '' } ] };
    const defaultDeployRepo = dctx.deploy_repo || '';

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';

    const title = document.createElement('div');
    title.className = 'mb-2 font-semibold flex items-center gap-2 ' + accentT;
    const subText = isProd
        ? '— current prd/deployment.json; edit it, then submit STAGES these charts into the PRD release (promotes via SIT→UAT→PRD when released)'
        : '— current uat/deployment.json; edit (add/remove entries), then submit OVERRIDES the file with exactly what you see';
    title.innerHTML = '<i class="fa-solid ' + icon + '"></i> ' + heading +
        ' <span class="text-slate-400 font-normal text-xs">' + subText + '</span>';
    wrap.appendChild(title);
    const depNote = ctxNote(dctx, 'the live deployment.json'); if (depNote) wrap.appendChild(depNote);

    const taId = 'deploy-json-' + env;
    const ta = document.createElement('textarea');
    ta.id = taId;
    ta.rows = 12;
    ta.spellcheck = false;
    ta.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none mb-2';
    ta.value = JSON.stringify(fileDoc, null, 2);
    wrap.appendChild(ta);

    // Target deployment repo — part of the deploy JSON payload.
    const repoBox = document.createElement('div');
    repoBox.className = 'mb-2';
    const repoLabel = document.createElement('label');
    repoLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    repoLabel.textContent = 'Deployment repo (owner/repo)';
    const repoInput = document.createElement('input');
    repoInput.id = 'deploy-repo-' + env;
    repoInput.type = 'text';
    repoInput.placeholder = 'e.g. my-org/deployment-repo';
    repoInput.value = defaultDeployRepo;
    repoInput.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
    repoBox.appendChild(repoLabel); repoBox.appendChild(repoInput);
    wrap.appendChild(repoBox);

    // PROD requires a change request — feeds change-request.json in the release PR.
    if (isProd) {
        const hdr = document.createElement('div');
        hdr.className = 'text-[11px] font-semibold text-amber-300 mt-1 mb-1';
        hdr.textContent = 'Change request (required for PROD)';
        wrap.appendChild(hdr);
        const grid = document.createElement('div');
        grid.className = 'grid gap-2 mb-2';
        const field = (labelText, el, id, type) => {
            if (type) el.type = type;
            el.id = id;
            el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
            const l = document.createElement('label');
            l.className = 'text-[11px] text-slate-400 block mb-0.5';
            l.textContent = labelText;
            const box = document.createElement('div');
            box.appendChild(l); box.appendChild(el);
            grid.appendChild(box);
        };
        field('Change summary', document.createElement('input'), 'chg-summary-' + env, 'text');
        field('Change description', document.createElement('textarea'), 'chg-desc-' + env);
        field('Start time', document.createElement('input'), 'chg-start-' + env, 'datetime-local');
        field('End time', document.createElement('input'), 'chg-end-' + env, 'datetime-local');
        wrap.appendChild(grid);
    }

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = accentBtn + ' px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = heading;
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';

    submit.addEventListener('click', () => {
        err.textContent = '';
        const parsed = parseDeployInclude(document.getElementById(taId).value);
        if (!parsed || !parsed.include.length) {
            err.textContent = 'Could not find any chart entries — each needs helm_chart_name + helm_chart_version.';
            return;
        }
        for (const it of parsed.include) {
            if (!it || !it.helm_chart_name || !it.helm_chart_version) {
                err.textContent = 'Each entry needs a non-empty helm_chart_name + helm_chart_version.';
                return;
            }
        }
        const deployRepo = document.getElementById('deploy-repo-' + env).value.trim();
        if (!deployRepo) {
            err.textContent = 'Deployment repo is required (owner/repo).';
            return;
        }
        const payload = { environment: env, include: parsed.include, deployment_repo: deployRepo };
        if (isProd) {
            const summary = document.getElementById('chg-summary-' + env).value.trim();
            const description = document.getElementById('chg-desc-' + env).value.trim();
            const startRaw = document.getElementById('chg-start-' + env).value;
            const endRaw = document.getElementById('chg-end-' + env).value;
            if (!summary || !description || !startRaw || !endRaw) {
                err.textContent = 'PROD requires change summary, description, start time, and end time.';
                return;
            }
            const start = new Date(startRaw), end = new Date(endRaw);
            if (!(end.getTime() > start.getTime())) {
                err.textContent = 'Change end time must be after the start time.';
                return;
            }
            // datetime-local is browser-local; store as ISO-8601 UTC.
            payload.change_request = {
                chg_summary: summary,
                description: description,
                start_date: start.toISOString(),
                end_date: end.toISOString(),
            };
        }
        // Re-render the normalized JSON so the user sees exactly what we parsed
        // (commas added / wrapped into include[] when they left them out).
        document.getElementById(taId).value = JSON.stringify({ include: parsed.include }, null, 2);
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit);
    row.appendChild(err);
    wrap.appendChild(row);

    withDismiss(wrap);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}
