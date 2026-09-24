import { deployTemplatePath, getContext } from '../api.js';
import { sendMessage } from '../chat.js';
import { showBqCost } from './bq_cost.js';
import { ctxNote, labeledField, opening, withDismiss } from './common.js';
import { showDfDeployForm } from './df_deploy_form.js';
import { showMonitoring } from './monitoring.js';
import { parseDeployInclude } from './parse.js';
import { showQueueForm } from './queue_form.js';
import { showQueueTable } from './queue_table.js';
import { showReleaseHistory } from './release_history.js';
import { showReleaseForm } from './release_form.js';

// Deploy editor — shows the ACTUAL current deployment.json as editable JSON
// (pre-filled from /api/deploy-template, which reads the live uat/ file; a chart
// named from a chat command is upserted in). On submit it sends
// {environment, include} through /api/chat; the backend previews the exact JSON
// it will write and asks to confirm.
//
// UAT ONLY. There is no PROD variant: everything ships through the intake queue
// → CARE/DF release → promote, so PROD is reached by promoting the release
// file-set (with its change request), never by editing prd/deployment.json for
// one chart. A typed prod deploy is answered with that route in
// core/deploy_routing.js, and the backend refuses it as well.
export async function showDeployForm(target, name, version) {
    if (target === 'df-uat') { showDfDeployForm(); return; }
    if (target === 'release') { showReleaseForm(); return; }
    if (target === 'df-release') { showReleaseForm('df'); return; }
    if (target === 'queue') { showQueueForm(); return; }
    if (target === 'queue-table') { showQueueTable(); return; }
    if (target === 'queue-remove') { showQueueTable({ remove: true }); return; }
    if (target === 'release-history') { showReleaseHistory(); return; }
    if (target === 'monitoring') { showMonitoring(); return; }
    if (target === 'bq-cost') { showBqCost(); return; }
    // The only environment this form can write, whatever was asked for — the
    // payload's environment is fixed here rather than taken from the caller.
    const env = 'uat';
    // "CARE UAT" distinguishes the helm-chart lane from the Dataflow one, which
    // deploys to the same environment by a different mechanism.
    const heading = 'Deploy to CARE UAT';

    // Pre-fill the editor with the WHOLE current deployment.json ({"include":[...]})
    // from the backend (the live uat/ file) — edit entries, add more to deploy
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
    title.className = 'mb-2 font-semibold flex items-center gap-2 text-emerald-300';
    const subText = '— current uat/deployment.json; edit (add/remove entries), then submit OVERRIDES the file with exactly what you see';
    title.innerHTML = '<i class="fa-solid fa-flask"></i> ' + heading +
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
    labeledField(wrap, {
        label: 'Deployment repo (owner/repo)',
        id: 'deploy-repo-' + env,
        placeholder: 'e.g. my-org/deployment-repo',
        value: defaultDeployRepo,
        boxClass: 'mb-2',
    });

    // No change-request fields here: the change request belongs to the release
    // (release_form.js), which is the only way to PROD.

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium';
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
