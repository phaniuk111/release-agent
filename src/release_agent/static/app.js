let threadId = localStorage.getItem('thread_id') || 'fastapi-' + Math.random().toString(36).slice(2, 10);
        localStorage.setItem('thread_id', threadId);
        document.getElementById('thread-label').textContent = threadId;

        // Base path so the UI works at "/" AND under a shared-domain path prefix
        // (e.g. /release-copilot). Derived from where this page is served.
        const API_BASE = (function () {        // strip trailing slashes (regex-free)
            let p = window.location.pathname;
            while (p.endsWith('/')) p = p.slice(0, -1);
            return p;
        })();

        // Minimal, safe markdown -> HTML for streamed assistant text.
        function renderMarkdown(t) {
            t = t.split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;');
            // [text](url) markdown links -> stash so the bare-URL linkifier below
            // doesn't double-wrap the URL inside the href attribute.
            const _links = [];
            t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function(m, txt, url) {
                _links.push('<a href="' + url + '" target="_blank" class="underline text-emerald-400">' + txt + '</a>');
                return 'LINKTOKEN' + (_links.length - 1) + 'ENDTOKEN';
            });
            t = t.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" class="underline text-emerald-400">$1</a>');
            t = t.replace(/LINKTOKEN(\d+)ENDTOKEN/g, function(m, i) { return _links[+i]; });
            t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
            t = t.replace(/`([^`]+)`/g, '<code class="bg-slate-800 px-1 rounded text-emerald-300">$1</code>');
            t = t.split('\n').join('<br>');
            return t;
        }

        function addMessage(role, content, isStreaming = false) {
            const chat = document.getElementById('chat');
            const div = document.createElement('div');
            
            if (role === 'interrupt') {
                // content may be the full interrupt object (preferred) or a bare string.
                const intr = (content && typeof content === 'object') ? content : { message: content };
                const isBudget = intr.type === 'budget_confirmation';
                const header = isBudget ? 'Budget Confirmation' : 'Confirmation Required';
                const bodyText = renderMarkdown(intr.message || 'Please confirm this action.')
                    + (intr.action ? ('<br><br>' + renderMarkdown(intr.action)) : '');
                const placeholder = isBudget
                    ? 'Type yes to continue, anything else to stop'
                    : 'Paste CONFIRM-XXXXXX here';
                div.className = 'message mx-auto interrupt-box rounded-2xl p-4 text-sm';
                div.innerHTML = `
                    <div class="flex items-center gap-2 mb-2 text-amber-400">
                        <i class="fa-solid fa-exclamation-triangle"></i>
                        <span class="font-semibold">${header}</span>
                    </div>
                    <div class="text-amber-200 mb-3">${bodyText}</div>
                    <div class="flex gap-2">
                        <input id="confirm-input" type="text" placeholder="${placeholder}"
                               class="flex-1 bg-slate-900 border border-amber-600 rounded-lg px-3 py-1.5 text-sm">
                        <button onclick="sendConfirmation()"
                                class="bg-amber-600 hover:bg-amber-500 px-4 rounded-lg text-sm font-medium">
                            Confirm
                        </button>
                    </div>
                `;
            } else {
                div.className = `message ${role === 'user' ? 'ml-auto user' : 'bot'} rounded-2xl px-4 py-3 text-sm`;
                div.innerHTML = `<div class="${isStreaming ? 'streaming' : ''}">${content}</div>`;
            }
            
            chat.appendChild(div);
            chat.scrollTop = chat.scrollHeight;
            return div;
        }

        function updateLastMessage(content) {
            const chat = document.getElementById('chat');
            const last = chat.lastElementChild;
            if (last) {
                const contentDiv = last.querySelector('div');
                if (contentDiv) contentDiv.innerHTML = content;
            }
        }

        async function sendMessage(overrideText) {
            const input = document.getElementById('input');
            // overrideText lets callers send multi-line messages (the single-line
            // text input strips newlines, which breaks the PROD change-ticket form).
            const message = (typeof overrideText === 'string' ? overrideText : input.value).trim();
            if (!message) return;

            // A deploy command typed in the chat box opens the editable JSON instead
            // of going straight to the agent (the JSON payload from the editor, which
            // starts with '{', is sent normally).
            if (!message.startsWith('{')) {
                const di = parseDeployIntent(message);
                if (di) {
                    if (typeof overrideText !== 'string') input.value = '';
                    addMessage('user', message);
                    showDeployForm(di.env, di.name, di.version);
                    return;
                }
            }

            addMessage('user', message);
            if (typeof overrideText !== 'string') input.value = '';

            const botMsg = addMessage('bot', '<span class="dots"><span></span><span></span><span></span></span>', true);

            try {
                const res = await fetch(API_BASE + '/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message, thread_id: threadId })
                });

                if (!res.ok) throw new Error(await res.text());

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let fullText = '';
                let isInterrupt = false;
                let buffer = '';

                function handleEvent(rawEvent) {
                    for (const line of rawEvent.split('\n')) {
                        if (!line.startsWith('data: ')) continue;
                        try {
                            const data = JSON.parse(line.slice(6));
                            if (data.type === 'token') {
                                fullText += (fullText ? '\n\n' : '') + data.content;
                                botMsg.querySelector('div').innerHTML = renderMarkdown(fullText);
                            } else if (data.type === 'interrupt') {
                                isInterrupt = true;
                                botMsg.remove();
                                addMessage('interrupt', data.data || {});
                            } else if (data.type === 'done') {
                                // finished
                            } else if (data.type === 'error') {
                                botMsg.querySelector('div').innerHTML =
                                    '<span class="text-red-400">' + (data.content || 'Error') + '</span>';
                            }
                        } catch (e) { console.error('SSE parse error', e, line); }
                    }
                }

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    // Accumulate across reads; SSE events are delimited by a blank line.
                    // A frame split mid-line would otherwise be dropped by the silent catch.
                    buffer += decoder.decode(value, { stream: true });
                    let sep;
                    while ((sep = buffer.indexOf('\n\n')) !== -1) {
                        const rawEvent = buffer.slice(0, sep);
                        buffer = buffer.slice(sep + 2);
                        handleEvent(rawEvent);
                    }
                }
                buffer += decoder.decode();
                if (buffer.trim()) handleEvent(buffer);

                if (!isInterrupt && botMsg) {
                    botMsg.querySelector('div').classList.remove('streaming');
                }
                // A turn may have raised/blocked a PRD PR — refresh the window banner.
                loadReleaseStatus();
            } catch (err) {
                botMsg.querySelector('div').innerHTML = `<span class="text-red-400">Error: ${err.message}</span>`;
            }
        }

        function sendConfirmation() {
            const input = document.getElementById('confirm-input');
            if (!input) return;
            const value = input.value.trim();
            if (!value) return;

            // Send the confirmation token as a regular message
            const chat = document.getElementById('chat');
            // Remove the interrupt box
            const last = chat.lastElementChild;
            if (last) last.remove();

            // Send as normal message
            const hiddenInput = document.getElementById('input');
            hiddenInput.value = value;
            sendMessage();
        }

        async function newThread() {
            // Drop the old thread's stored repo + PAT on the server, then rotate.
            try {
                await fetch(API_BASE + '/api/session/disconnect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ thread_id: threadId })
                });
            } catch (e) {}
            threadId = 'fastapi-' + Math.random().toString(36).slice(2, 10);
            localStorage.setItem('thread_id', threadId);
            document.getElementById('thread-label').textContent = threadId;
            document.getElementById('chat').innerHTML = '';
            renderConnectionStatus({ connected: false });
            addMessage('bot', 'New conversation started. How can I help with releases?');
            showConnectForm();
        }

        // ---- Repository + PAT connection (per session) -------------------------
        // Non-secret repo/branch/project are cached in localStorage for convenience;
        // the PAT is sent once to the server and never stored in the browser.
        function renderConnectionStatus(s) {
            const icon = document.getElementById('repo-chip-icon');
            const label = document.getElementById('repo-chip-label');
            if (!icon || !label) return;
            if (s && s.connected && s.repo) {
                icon.className = 'fa-solid fa-link text-emerald-400';
                label.textContent = s.repo + (s.branch ? ('@' + s.branch) : '');
                label.className = 'text-emerald-300';
            } else {
                icon.className = 'fa-solid fa-link-slash text-slate-400';
                label.textContent = 'Connect repo';
                label.className = 'text-slate-300';
            }
        }

        async function refreshConnectionStatus() {
            try {
                const r = await fetch(API_BASE + '/api/session/status?thread_id=' + encodeURIComponent(threadId));
                if (r.ok) renderConnectionStatus(await r.json());
            } catch (e) {}
        }

        function showConnectForm() {
            const chat = document.getElementById('chat');
            const cached = JSON.parse(localStorage.getItem('repo_conn') || '{}');
            const wrap = document.createElement('div');
            wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';
            wrap.innerHTML =
                '<div class="mb-2 font-semibold flex items-center gap-2 text-emerald-300">' +
                '<i class="fa-solid fa-plug"></i> Connect a repository</div>' +
                '<div class="text-slate-400 text-xs mb-3">Provide the repo and a GitHub PAT to run against your own repository this session. ' +
                'The token stays in memory on the server, is never logged, and is dropped when you start a new thread.</div>';

            const grid = document.createElement('div');
            grid.className = 'grid gap-2 mb-2';
            const mk = (labelText, id, type, placeholder, value) => {
                const l = document.createElement('label');
                l.className = 'text-[11px] text-slate-400 block mb-0.5';
                l.textContent = labelText;
                const el = document.createElement('input');
                el.id = id; el.type = type; el.placeholder = placeholder || '';
                if (value) el.value = value;
                el.className = 'w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none';
                const box = document.createElement('div');
                box.appendChild(l); box.appendChild(el);
                grid.appendChild(box);
            };
            mk('Repository (owner/repo or URL)', 'conn-repo', 'text', 'e.g. octocat/hello-world', cached.repo);
            mk('Branch name (optional)', 'conn-branch', 'text', 'e.g. main', cached.branch);
            mk('PAT token', 'conn-pat', 'password', 'ghp_… (never stored in the browser)', '');
            mk('Project name (optional)', 'conn-project', 'text', '', cached.project_name);
            wrap.appendChild(grid);

            const row = document.createElement('div');
            row.className = 'flex items-center gap-3 mt-1';
            const submit = document.createElement('button');
            submit.className = 'bg-emerald-600 hover:bg-emerald-500 px-4 py-1.5 rounded-lg text-sm font-medium';
            submit.textContent = 'Connect';
            const err = document.createElement('span');
            err.className = 'text-[11px] text-red-400';

            submit.addEventListener('click', async () => {
                err.textContent = '';
                const repo = document.getElementById('conn-repo').value.trim();
                const branch = document.getElementById('conn-branch').value.trim();
                const pat = document.getElementById('conn-pat').value.trim();
                const project = document.getElementById('conn-project').value.trim();
                if (!repo) { err.textContent = 'Repository is required.'; return; }
                if (!pat) { err.textContent = 'PAT token is required.'; return; }
                submit.disabled = true; submit.textContent = 'Connecting…';
                try {
                    const r = await fetch(API_BASE + '/api/session/connect', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ thread_id: threadId, repo, branch, pat_token: pat, project_name: project })
                    });
                    const d = await r.json();
                    if (!d.ok) { err.textContent = d.error || 'Could not connect.'; return; }
                    // Cache non-secret fields only (never the PAT).
                    localStorage.setItem('repo_conn', JSON.stringify({ repo: d.repo, branch: d.branch, project_name: d.project_name }));
                    renderConnectionStatus(d);
                    wrap.remove();
                    addMessage('bot', 'Connected to <code>' + d.repo + '</code>' +
                        (d.branch ? (' on <code>' + d.branch + '</code>') : '') +
                        ' (token ' + (d.token_preview || 'set') + '). I\'ll use this repository for GitHub actions this session.');
                } catch (e) {
                    err.textContent = 'Network error: ' + e.message;
                } finally {
                    submit.disabled = false; submit.textContent = 'Connect';
                }
            });
            row.appendChild(submit); row.appendChild(err);
            wrap.appendChild(row);
            chat.appendChild(wrap);
            chat.scrollTop = chat.scrollHeight;
        }

        // Quick actions — what the agent can do. mode 'send' runs immediately;
        // otherwise the text is pre-filled so the user edits the image:tag first.
        const CAPABILITIES = [
            {icon:'fa-flask',             label:'Deploy to UAT',        desc:'deploy a Helm chart to UAT',                  form:'uat'},
            {icon:'fa-shield-halved',     label:'Deploy to PROD',       desc:'deploy a Helm chart to PROD',                  form:'prod'},
            {icon:'fa-shield-heart',      label:'Release to PROD',      desc:'promote the PRD release via SIT→UAT→PRD (after cutoff)',  send:true,  text:'release prod'},
            {icon:'fa-eraser',            label:'Remove from release',  desc:'unstage a chart before it ships',             send:false, text:"remove <chart-name> from the release"},
            {icon:'fa-calendar-day',      label:'Deploy status',        desc:'UAT, PRD & the release PR',                   send:true,  text:'what is the current deploy status of UAT, PRD and the PRD release PR?'},
            {icon:'fa-circle-check',      label:'Verify a build',       desc:'tag-gen step + RLFT controls for a tag',      send:false, text:'verify <image>:<tag> was built in <owner/repo>'},
            {icon:'fa-list-check',        label:'Check PRD controls',   desc:'pass/fail RLFT/RFTL gates for a tag',         send:false, text:'check build controls for <image>:<tag> before a PRD release'},
            {icon:'fa-images',            label:'List allowed images',  desc:'what I can promote',                          send:true,  text:'what images can I promote?'},
            {icon:'fa-clock-rotate-left', label:'Recent workflow runs', desc:'status of the latest runs',                   send:true,  text:'show me the 5 most recent workflow runs and their status'},
            {icon:'fa-code-pull-request', label:'Track a PR',           desc:'find the PR & summarize CHG/RMG/RLFT',         send:false, text:'find the deployment PR for <image>:<tag> and summarize its CHG, RMG and RLFT controls'},
            {icon:'fa-rotate',            label:'Re-run a step',        desc:'re-run apply or dispatch',                    send:false, text:'re-run dispatch_workflow'},
        ];

        function runQuick(text, send) {
            if (send) {
                sendMessage(text);   // send directly so multi-line messages keep their newlines
                return;
            }
            const input = document.getElementById('input');
            input.value = text;
            input.focus();
            try { input.setSelectionRange(text.length, text.length); } catch (e) {}
        }

        function showCapabilities() {
            const chat = document.getElementById('chat');
            const wrap = document.createElement('div');
            wrap.className = 'message bot rounded-2xl p-4 text-sm';

            const title = document.createElement('div');
            title.className = 'mb-2 text-slate-300 font-semibold';
            title.textContent = 'What I can do — pick one to start:';
            wrap.appendChild(title);

            const grid = document.createElement('div');
            grid.className = 'grid grid-cols-1 sm:grid-cols-2 gap-2';
            CAPABILITIES.forEach(c => {
                const btn = document.createElement('button');
                btn.className = 'text-left bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-xl px-3 py-2 flex items-start gap-2';
                btn.innerHTML = '<i class="fa-solid ' + c.icon + ' text-emerald-400 mt-1"></i>' +
                    '<span><span class="font-medium">' + c.label + '</span><br>' +
                    '<span class="text-[11px] text-slate-400">' + c.desc + '</span></span>';
                btn.addEventListener('click', () => c.form ? showDeployForm(c.form) : runQuick(c.text, c.send));
                grid.appendChild(btn);
            });
            wrap.appendChild(grid);

            const note = document.createElement('div');
            note.className = 'text-[10px] text-slate-500 mt-2';
            note.textContent = 'Deploy opens an editable JSON entry; some actions run immediately; others pre-fill the box so you can edit, then Send.';
            wrap.appendChild(note);

            chat.appendChild(wrap);
            chat.scrollTop = chat.scrollHeight;
        }

        // Deploy form — three inputs: chart name, version, namespace.
        // On submit sends a JSON string through the normal /api/chat SSE flow.
        // The backend parses the JSON, assembles the Helm entry, previews it,
        // and replies with a CONFIRM-XXXXXX token; the existing interrupt UI
        // then handles the confirmation step unchanged.
        // Deploy editor — shows the ACTUAL current deployment.json as editable JSON
        // (pre-filled from /api/deploy-template, which reads the live uat/ or prd file;
        // a chart named from a chat command is upserted in). On submit it sends
        // {environment, include} through /api/chat; the backend previews the exact JSON
        // it will write and asks to confirm.
        async function showDeployForm(env, name, version) {
            const isProd = env === 'prod';
            const accentT = isProd ? 'text-amber-300' : 'text-emerald-300';
            const accentBtn = isProd ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500';
            const icon = isProd ? 'fa-shield-halved' : 'fa-flask';
            const heading = isProd ? 'Deploy to PROD' : 'Deploy to UAT';

            // Pre-fill the editor with the WHOLE current deployment.json ({"include":[...]})
            // from the backend (the live uat/ or prd file) — edit entries, add more to deploy
            // several charts at once. The fallback below is only used if the fetch fails.
            let fileDoc = { include: [ { helm_chart_name: name || '', helm_chart_version: version || '' } ] };
            try {
                const qs = new URLSearchParams({ env: env, name: name || '', version: version || '' });
                const r = await fetch(API_BASE + '/api/deploy-template?' + qs.toString());
                if (r.ok) { const d = await r.json(); fileDoc = d.deployment; }
            } catch (e) {}

            const chat = document.getElementById('chat');
            const wrap = document.createElement('div');
            wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm';

            const title = document.createElement('div');
            title.className = 'mb-2 font-semibold flex items-center gap-2 ' + accentT;
            const subText = isProd
                ? '— current prd/deployment.json; edit it, then submit STAGES these charts into the PRD release (promotes via SIT→UAT→PRD at the cutoff)'
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
                const payload = { environment: env, include: parsed.include };
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
        function parseDeployIntent(text) {
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
        function parseDeployInclude(text) {
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

        // Release status panel — reads the PRD-release-PR API shape:
        // { date_utc, now_utc, cutoff_utc, cutoff_passed, uat_charts, prd_charts,
        //   prd_release_pr: {number,url,charts,can_merge_now}, pending_to_prod, reason }
        function _chartList(arr) {
            return (arr || []).map(function(c){ return c.helm_chart_name + ':' + c.helm_chart_version; })
                .join(' &nbsp;│&nbsp; ');
        }
        async function loadReleaseStatus() {
            const banner = document.getElementById('release-banner');
            const icon   = document.getElementById('rb-icon');
            const title  = document.getElementById('rb-title');
            const detail = document.getElementById('rb-detail');
            try {
                const res = await fetch(API_BASE + '/api/release-status');
                const s = await res.json();
                banner.classList.remove('hidden');
                banner.className = 'mb-4 rounded-2xl border px-4 py-3 text-sm';
                if (s.error) {
                    banner.classList.add('border-slate-700', 'bg-slate-900');
                    icon.className = 'fa-solid fa-triangle-exclamation text-slate-400';
                    title.textContent = "Couldn't fetch release status";
                    detail.textContent = s.error;
                    return;
                }
                const foot = 'cutoff ' + s.cutoff_utc + ' UTC • now ' + s.now_utc + ' • ' + s.date_utc;
                const pr = s.prd_release_pr;
                if (pr) {
                    const n = (s.pending_to_prod || []).length;
                    const plural = n === 1 ? '' : 's';
                    if (pr.can_merge_now) {
                        banner.classList.add('border-emerald-600/50', 'bg-emerald-500/10');
                        icon.className = 'fa-solid fa-circle-check text-emerald-400';
                        title.textContent = '🟢 PRD release PR #' + pr.number + ' ready to release (' + n + ' change' + plural + ')';
                    } else {
                        banner.classList.add('border-amber-600/50', 'bg-amber-500/10');
                        icon.className = 'fa-solid fa-rocket text-amber-400';
                        title.textContent = '🚀 PRD release PR #' + pr.number + ' collecting (' + n + ' change' + plural + ')';
                    }
                    let html = (s.reason ? s.reason + ' • ' : '') + foot;
                    if ((pr.charts || []).length) {
                        html += '<br><span class="text-slate-400">staged: ' + _chartList(pr.charts) + '</span>';
                    }
                    html += ' &nbsp;<a href="' + pr.url + '" target="_blank" class="underline text-emerald-400">open PR #' + pr.number + '</a>';
                    detail.innerHTML = html;
                    return;
                }
                // No PRD release PR open today.
                banner.classList.add('border-slate-700', 'bg-slate-900');
                icon.className = 'fa-solid fa-circle-check text-slate-400';
                title.textContent = 'No PRD release open today';
                detail.innerHTML = (s.reason ? s.reason + ' • ' : '') + foot;
            } catch (e) {
                banner.classList.remove('hidden');
                icon.className = 'fa-solid fa-triangle-exclamation text-slate-400';
                title.textContent = "Couldn't reach the release-status endpoint";
                detail.textContent = String(e);
            }
        }

        // Welcome message
        window.onload = () => {
            const chat = document.getElementById('chat');
            if (chat.children.length === 0) {
                addMessage('bot', 'Hello! I can help you deploy Helm charts and manage release workflows.');
                showCapabilities();
            }
            refreshConnectionStatus();
            loadReleaseStatus();
            // Keep it fresh so a release raised in another session shows up here.
            setInterval(loadReleaseStatus, 60000);
        };

        // Enter key support
        document.getElementById('input').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') sendMessage();
        });
