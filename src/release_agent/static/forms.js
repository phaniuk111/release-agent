// Deploy forms: the GKE JSON editor, the Dataflow image+tag form, and the
// client-side parsers that pop them from typed chat commands. On submit each
// form sends a JSON payload through the normal /api/chat SSE flow; the backend
// previews the exact change and asks for the CONFIRM token.
import { API_BASE } from './state.js';
import { sendMessage } from './chat.js';

// ---- Dataflow flex-template deploy (workflow-dispatch golden path) ------
// The dev supplies image name + tag; deploying dispatches the DF repo's
// deploy workflow. Recent runs give context; repo override sits under
// Advanced. Same preview + CONFIRM contract as every other deploy.
export async function showDfDeployForm() {
    let ctx = { recent_runs: [], deploy_repo: '', workflow: 'df-deploy.yml' };
    try {
        const r = await fetch(API_BASE + '/api/df-template?env=uat');
        if (r.ok) ctx = await r.json();
    } catch (e) {}

    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
    wrap.innerHTML =
        '<div class="mb-1 font-semibold flex items-center gap-2 text-sky-300">' +
        '<i class="fa-solid fa-water"></i> Deploy to DF UAT</div>' +
        '<div class="text-slate-400 text-xs mb-3">Triggers the <code>' + (ctx.workflow || 'df-deploy.yml') +
        '</code> workflow with your image + tag. Nothing runs until you confirm the preview.</div>';

    if ((ctx.recent_runs || []).length) {
        const hdr = document.createElement('div');
        hdr.className = 'text-[11px] text-slate-500 mb-1';
        hdr.textContent = 'Recent DF deploys';
        wrap.appendChild(hdr);
        const list = document.createElement('div');
        list.className = 'border border-slate-700 rounded-lg px-3 py-1.5 mb-3 text-[11px] font-mono text-slate-400';
        ctx.recent_runs.forEach(r => {
            const row = document.createElement('div');
            row.className = 'flex justify-between py-0.5';
            row.innerHTML = '<a href="' + r.url + '" target="_blank" class="underline">run #' + r.id + '</a>' +
                '<span>' + (r.conclusion || r.status || '') + '</span>';
            list.appendChild(row);
        });
        wrap.appendChild(list);
    }

    const grid = document.createElement('div');
    grid.className = 'grid grid-cols-2 gap-2 mb-1';
    const mk = (labelText, id, placeholder) => {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = labelText;
        const el = document.createElement('input');
        el.id = id; el.type = 'text'; el.placeholder = placeholder;
        el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
        const box = document.createElement('div');
        box.appendChild(l); box.appendChild(el);
        grid.appendChild(box);
        return el;
    };
    const imgEl = mk('Image name', 'df-image', 'e.g. order-enrichment');
    const tagEl = mk('Tag', 'df-tag', 'e.g. 1.4.2');
    wrap.appendChild(grid);

    const echo = document.createElement('div');
    echo.className = 'text-[11px] text-slate-500 mb-2 h-4';
    wrap.appendChild(echo);
    const updateEcho = () => {
        const i = imgEl.value.trim(), t = tagEl.value.trim();
        echo.textContent = (i && t) ? ('↳ will dispatch ' + i + ':' + t + ' → uat ✓') : '';
        echo.className = 'text-[11px] mb-2 h-4 ' + ((i && t) ? 'text-emerald-400' : 'text-slate-500');
    };
    imgEl.addEventListener('input', updateEcho);
    tagEl.addEventListener('input', updateEcho);

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
        if (!image || !tag) { err.textContent = 'Image name and tag are both required.'; return; }
        const payload = { deployment_type: 'dataflow', environment: 'uat', image: image, tag: tag };
        const repoOverride = repoInput.value.trim();
        if (repoOverride) payload.deployment_repo = repoOverride;
        sendMessage(JSON.stringify(payload));
    });
    row.appendChild(submit); row.appendChild(err);
    wrap.appendChild(row);
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// Deploy editor — shows the ACTUAL current deployment.json as editable JSON
// (pre-filled from /api/deploy-template, which reads the live uat/ or prd file;
// a chart named from a chat command is upserted in). On submit it sends
// {environment, include} through /api/chat; the backend previews the exact JSON
// it will write and asks to confirm.
export async function showDeployForm(env, name, version) {
    if (env === 'df-uat') { showDfDeployForm(); return; }
    const isProd = env === 'prod';
    const accentT = isProd ? 'text-amber-300' : 'text-emerald-300';
    const accentBtn = isProd ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500';
    const icon = isProd ? 'fa-shield-halved' : 'fa-flask';
    const heading = isProd ? 'Deploy to PROD' : 'Deploy to UAT';

    // Pre-fill the editor with the WHOLE current deployment.json ({"include":[...]})
    // from the backend (the live uat/ or prd file) — edit entries, add more to deploy
    // several charts at once. The fallback below is only used if the fetch fails.
    let fileDoc = { include: [ { helm_chart_name: name || '', helm_chart_version: version || '' } ] };
    let defaultDeployRepo = '';
    try {
        const qs = new URLSearchParams({ env: env, name: name || '', version: version || '' });
        const r = await fetch(API_BASE + '/api/deploy-template?' + qs.toString());
        if (r.ok) { const d = await r.json(); fileDoc = d.deployment; defaultDeployRepo = d.deploy_repo || ''; }
    } catch (e) {}

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

    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
}

// Detect a deploy command typed in the chat box so we can pop the editable
// JSON instead of sending it straight to the agent. Needs a deploy verb, a
// target env, and a <name>:<version> token.
// Regex-free tokenizers (mirror the Python no-regex parsing style).
function _isAlnum(ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9');
}
function _wordSet(text) {            // lowercased alphanumeric words
    const words = new Set();
    let cur = '';
    for (const ch of text) {
        if (_isAlnum(ch)) { cur += ch.toLowerCase(); }
        else { if (cur) words.add(cur); cur = ''; }
    }
    if (cur) words.add(cur);
    return words;
}
function _wsTokens(text) {           // whitespace-separated raw tokens
    const out = [];
    let cur = '';
    for (const ch of text) {
        if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') { if (cur) out.push(cur); cur = ''; }
        else { cur += ch; }
    }
    if (cur) out.push(cur);
    return out;
}
export function parseDeployIntent(text) {
    const w = _wordSet(text);
    const hasVerb = w.has('deploy') || w.has('promote') || w.has('ship') ||
                    w.has('rollout') || (w.has('roll') && w.has('out'));
    if (!hasVerb) return null;
    const env = (w.has('prod') || w.has('prd') || w.has('production')) ? 'prod'
              : (w.has('uat') ? 'uat' : null);
    if (!env) return null;
    // Find a <name>:<version> (or name=version) token without regex.
    for (const tok of _wsTokens(text)) {
        let i = tok.indexOf(':');
        if (i === -1) i = tok.indexOf('=');
        if (i <= 0) continue;
        const name = tok.slice(0, i);
        let version = tok.slice(i + 1);
        while (version && '.,;:)'.indexOf(version[version.length - 1]) !== -1) version = version.slice(0, -1);
        const c = name[0];
        if (((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) && version) {
            return { env: env, name: name, version: version };
        }
    }
    return null;
}

// Tolerant deploy-JSON parser. Accepts a clean {"include":[...]}, a bare array,
// or a single entry; if strict JSON.parse fails (e.g. the user pasted objects
// with no commas and no include[] wrapper), it brace-scans every balanced {...}
// and keeps the chart-entry-shaped ones. Returns {include, recovered} or null.
function _extractJsonObjects(text) {
    const out = [];
    for (let i = 0; i < text.length; i++) {
        if (text[i] !== '{') continue;
        let depth = 0, inStr = false, esc = false, end = -1;
        for (let j = i; j < text.length; j++) {
            const ch = text[j];
            if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
            if (ch === '"') inStr = true;
            else if (ch === '{') depth++;
            else if (ch === '}') { depth--; if (depth === 0) { end = j; break; } }
        }
        if (end === -1) break;
        try {
            const e = JSON.parse(text.slice(i, end + 1));
            if (e && typeof e === 'object' && !Array.isArray(e) &&
                (e.helm_chart_name !== undefined || e.helm_chart_version !== undefined)) {
                out.push(e);
            }
        } catch (_) { /* this {...} isn't a standalone object — skip */ }
    }
    return out;
}
export function parseDeployInclude(text) {
    text = (text || '').trim();
    try {
        const doc = JSON.parse(text);
        if (Array.isArray(doc)) return { include: doc, recovered: false };
        if (doc && Array.isArray(doc.include)) return { include: doc.include, recovered: false };
        if (doc && typeof doc === 'object' && doc.helm_chart_name !== undefined) return { include: [doc], recovered: false };
    } catch (_) { /* fall through to lenient recovery */ }
    const entries = _extractJsonObjects(text);
    return entries.length ? { include: entries, recovered: true } : null;
}
