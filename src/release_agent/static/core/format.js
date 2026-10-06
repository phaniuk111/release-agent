// Formatting shared by every screen. Pure: no DOM, no fetch, no globals — so
// the Node tests import it directly, and a React port (the Backstage plugin
// included) can use it unchanged.

// HTML-escape untrusted text before it enters innerHTML. Quotes MUST be escaped
// too: model output and tool results (PR titles, branch names, BQ notes) can
// carry a `"` that would otherwise break out of an attribute value — the
// indirect-prompt-injection path ADK's safety guidance calls out.
// (A React port does not need this: JSX escapes by default.)
export function escapeHtml(value) {
    return String(value == null ? '' : value)
        .split('&').join('&amp;')
        .split('<').join('&lt;')
        .split('>').join('&gt;')
        .split('"').join('&quot;')
        .split("'").join('&#39;');
}

// "5m ago" / "3h ago" / "2d ago" from an ISO timestamp; '' when there is none.
// `now` is injectable for tests.
export function timeAgo(iso, now = Date.now()) {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, (now - t) / 1000);
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
}

// "dev@example.com" -> "dev": how a requester is shown in tight columns.
export function shortName(email) {
    return String(email || '').split('@')[0];
}

// Punctuation that ends a sentence, not a URL: "… run #12 (https://x/runs/12)."
// must link https://x/runs/12 — found live, the link carried ")." and 404'd.
const URL_TRAIL = '.,;:!?';
// The text is HTML-escaped before links are made, so a quote after a URL
// arrives as an entity.
const URL_TRAIL_ENTITIES = ['&quot;', '&#39;', '&gt;', '&lt;'];

function count(text, ch) {
    let n = 0;
    for (const c of text) if (c === ch) n += 1;
    return n;
}

/**
 * Split a bare URL found in text into the URL and the punctuation after it.
 * A closing ")" or "]" stays when the URL opened one itself (…/Foo_(bar)).
 * @param {string} candidate
 * @returns {{url: string, rest: string}}
 */
export function splitUrl(candidate) {
    let url = String(candidate == null ? '' : candidate);
    let rest = '';
    while (url.length > 'https://'.length) {
        const entity = URL_TRAIL_ENTITIES.find(e => url.endsWith(e));
        if (entity) {
            url = url.slice(0, -entity.length);
            rest = entity + rest;
            continue;
        }
        const last = url.slice(-1);
        const unbalanced = (last === ')' && count(url, ')') > count(url, '('))
            || (last === ']' && count(url, ']') > count(url, '['));
        if (!URL_TRAIL.includes(last) && !unbalanced) break;
        url = url.slice(0, -1);
        rest = last + rest;
    }
    return { url, rest };
}
