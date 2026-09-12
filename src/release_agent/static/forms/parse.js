// Chat-box parsing that pops a form instead of sending a deploy command
// straight to the agent. Pure: no DOM, no fetch.

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
