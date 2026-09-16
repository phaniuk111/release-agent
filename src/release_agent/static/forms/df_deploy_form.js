import { escapeHtml as esc } from '../core/format.js';
import { dfTemplatePath, getContext } from '../api.js';
import { sendMessage } from '../chat.js';
import { ctxNote, opening, withDismiss } from './common.js';

// ---- Dataflow flex-template deploy (workflow-dispatch golden path) ------
// The dev supplies image name + tag; deploying dispatches the DF repo's
// deploy workflow. Recent runs give context; repo override sits under
// Advanced. Same preview + CONFIRM contract as every other deploy.
export async function showDfDeployForm() {
    const _ph = opening('Deploy to DF UAT');
    const ctx = await getContext(dfTemplatePath('uat'),
        { deploy_repo: '', workflow: 'df-deploy.yml' });
    _ph.remove();

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-1 font-semibold flex items-center gap-2 text-sky-300">' +
        '<i class="fa-solid fa-water"></i> Deploy to DF UAT</div>' +
        '<div class="text-slate-400 text-xs mb-3">Triggers the <code>' + esc(ctx.workflow || 'df-deploy.yml') +
        '</code> workflow. Nothing runs until you confirm the preview.</div>';

    const dfNote = ctxNote(ctx, 'recent DF runs'); if (dfNote) wrap.appendChild(dfNote);

    // Fields are labelled with the TARGET WORKFLOW's own input names (module /
    // binary_version, not our internal image / tag) and a `choice` input becomes
    // a dropdown — GitHub rejects any value outside its options:, so offering
    // free text there only produces a refusal after the developer confirms.
    const fields = ctx.fields || {};
    const fImage = fields.image || { name: 'image', label: 'Image name', options: [] };
    const fTag = fields.tag || { name: 'tag', label: 'Tag', options: [] };

    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-2 mb-1';
    const mk = (spec, id, fallbackLabel, placeholder) => {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = spec.label || fallbackLabel;
        const opts = spec.options || [];
        const el = document.createElement(opts.length ? 'select' : 'input');
        el.id = id;
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        if (opts.length) {
            const blank = document.createElement('option');
            blank.value = ''; blank.textContent = 'select ' + (spec.label || fallbackLabel).toLowerCase() + '…';
            el.appendChild(blank);
            opts.forEach(o => {
                const opt = document.createElement('option');
                opt.value = o; opt.textContent = o;
                el.appendChild(opt);
            });
            if (spec.default && opts.indexOf(spec.default) !== -1) el.value = spec.default;
        } else {
            el.type = 'text';
            el.placeholder = placeholder;
            if (spec.default) el.value = spec.default;
        }
        if (spec.description) el.title = spec.description;
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    const imgEl = mk(fImage, 'df-image', 'Image name', 'e.g. order-enrichment');
    const tagEl = mk(fTag, 'df-tag', 'Tag', 'e.g. 1.4.2');
    wrap.appendChild(grid);

    const echo = document.createElement('div');
    echo.className = 'text-[11px] text-slate-500 mb-2 h-4';
    wrap.appendChild(echo);
    const updateEcho = () => {
        const i = imgEl.value.trim(), t = tagEl.value.trim();
        // Echo the real dispatch inputs, matching what the preview will show.
        echo.textContent = (i && t)
            ? ('↳ will dispatch ' + fImage.name + '=' + i + ' ' + fTag.name + '=' + t + ' → uat ✓')
            : '';
        echo.className = 'text-[11px] mb-2 h-4 ' + ((i && t) ? 'text-emerald-400' : 'text-slate-500');
    };
    ['input', 'change'].forEach(ev => {
        imgEl.addEventListener(ev, updateEcho);
        tagEl.addEventListener(ev, updateEcho);
    });
    updateEcho();

    // Composer DAGs to point at the new template version. The developer names
    // the files — no picker to keep in sync with the repo, and a typo is caught
    // by the preview, which reads each file and reports it by name before the
    // CONFIRM token is issued.
    const dagLabel = document.createElement('label');
    dagLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    dagLabel.textContent = 'Composer DAG file(s) (optional) — one .py per line; ' +
        'their default template version is bumped to this tag via a PR';
    const dagEl = document.createElement('textarea');
    dagEl.id = 'df-dags'; dagEl.rows = 3; dagEl.spellcheck = false;
    dagEl.placeholder = ctx.composer_dir
        ? (ctx.composer_dir + '/…  e.g.\nacme-svc-alpha.py\nacme-svc-beta.py')
        : 'acme-svc-alpha.py';
    dagEl.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 ' +
        'text-xs text-white font-mono focus:outline-none mb-1';
    wrap.appendChild(dagLabel); wrap.appendChild(dagEl);
    // Which Composer repo those DAGs live in. Pre-filled from config, editable
    // for teams whose DAGs are not all in one place — same pattern as the
    // deployment-repo override on the release forms.
    const dagRepoLabel = document.createElement('label');
    dagRepoLabel.className = 'text-[11px] text-slate-400 block mb-0.5';
    dagRepoLabel.textContent = 'Composer DAGs repo (owner/repo)';
    const dagRepoEl = document.createElement('input');
    dagRepoEl.id = 'df-composer-repo'; dagRepoEl.type = 'text';
    dagRepoEl.value = ctx.composer_repo || '';
    dagRepoEl.placeholder = 'e.g. my-org/composer-dags';
    dagRepoEl.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 ' +
        'text-xs text-white focus:outline-none mb-1';
    wrap.appendChild(dagRepoLabel); wrap.appendChild(dagRepoEl);

    const dagHint = document.createElement('div');
    dagHint.className = 'text-[10px] text-slate-600 mb-2';
    dagHint.textContent = 'Files are read from ' + (ctx.composer_dir || '<env>') +
        '/ on the DAG branch. A PR is raised — nothing is merged for you.';
    wrap.appendChild(dagHint);

    // Advanced (collapsed): repo override.
    const adv = document.createElement('div');
    adv.className = 'mb-2';
    const advToggle = document.createElement('button');
    advToggle.className = 'text-[11px] text-slate-500 hover:text-slate-300';
    advToggle.innerHTML = '<i class="fa-solid fa-chevron-right"></i> Advanced — repo override';
    const advBody = document.createElement('div');
    advBody.className = 'hidden mt-1';
    const repoInput = document.createElement('input');
    repoInput.id = 'df-repo'; repoInput.type = 'text';
    repoInput.value = ctx.deploy_repo || '';
    repoInput.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
    advBody.appendChild(repoInput);
    advToggle.addEventListener('click', () => advBody.classList.toggle('hidden'));
    adv.appendChild(advToggle); adv.appendChild(advBody);
    wrap.appendChild(adv);

    const row = document.createElement('div');
    row.className = 'flex items-center gap-3 mt-1';
    const submit = document.createElement('button');
    submit.className = 'bg-sky-600 hover:bg-sky-500 px-4 py-1.5 rounded-lg text-sm font-medium';
    submit.textContent = 'Deploy to DF UAT';
    const err = document.createElement('span');
    err.className = 'text-[11px] text-red-400';
    submit.addEventListener('click', () => {
        err.textContent = '';
        const image = imgEl.value.trim(), tag = tagEl.value.trim();
        if (!image || !tag) {
            err.textContent = (fImage.label || 'Image name') + ' and ' +
                (fTag.label || 'tag').toLowerCase() + ' are both required.';
            return;
        }
        const payload = { deployment_type: 'dataflow', environment: 'uat', image: image, tag: tag };
        const dags = dagEl.value.split('\n').map(l => l.trim()).filter(Boolean);
        if (dags.length) {
            payload.dag_files = dags;
            const dagRepo = dagRepoEl.value.trim();
            if (!dagRepo) {
                err.textContent = 'Name the Composer DAGs repo (owner/repo) for those DAG files.';
                return;
            }
            payload.composer_repo = dagRepo;
        }
        const repoOverride = repoInput.value.trim();
        if (repoOverride) payload.deployment_repo = repoOverride;
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    withDismiss(wrap);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}
