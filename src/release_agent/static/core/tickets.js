// How a PR's CHG / RMG tickets read on the banner — the one rule both UIs share.
// Pure: no DOM, no fetch. The server's `tickets` object (tools/tickets.py) is
// one entry per prefix, `{chg: {id, source, url} | null, rmg: ...}`, or
// `{error}` when the PR could not be read.
//
// Each prefix becomes one part: the ticket (a link to where it was named) or,
// when the PR names none yet, a grey "no CHG yet" — the ticket a release still
// needs is as much a fact as the one it has. Only the ticket is shown, never a
// state: the comments do not say one. An error, or no tickets object at all, is
// no parts: the banner then shows the PR without tickets.

/**
 * Every CARE release PR open on a guarded branch — UAT→PRD and UAT→PRL1 run
 * side by side, each with its own CHG, so the banner lists each one. Falls back
 * to the single `blocking_pr` from a server that does not send `release_prs`.
 * @returns {{number, url, head, base, tickets?}[]}
 */
export function releasePrs(status) {
    const s = status && typeof status === 'object' ? status : {};
    const list = Array.isArray(s.release_prs) ? s.release_prs.filter(p => p && p.number) : [];
    if (list.length) return list;
    return s.blocking_pr && s.blocking_pr.number ? [s.blocking_pr] : [];
}

/** The prefixes shown, in banner order, when the server does not say. */
export const TICKET_PREFIXES = ['CHG', 'RMG'];

const norm = (v) => String(v == null ? '' : v).trim();

/**
 * One part per prefix.
 * @param {object|null|undefined} tickets  the server's tickets object
 * @param {string[]} [prefixes]  the prefixes to show, in order (default TICKET_PREFIXES)
 * @returns {{prefix: string, id: string, url: string, missing: boolean, text: string}[]}
 */
export function ticketParts(tickets, prefixes = TICKET_PREFIXES) {
    if (!tickets || typeof tickets !== 'object' || tickets.error) return [];
    return prefixes.map((prefix) => {
        const hit = tickets[prefix.toLowerCase()];
        const id = norm(hit && hit.id);
        if (!id) return { prefix, id: '', url: '', missing: true, text: 'no ' + prefix + ' yet' };
        return { prefix, id, url: norm(hit.url), missing: false, text: id };
    });
}

/**
 * The parts as one line: "CHG-12345 · RMG-678", "no CHG yet · no RMG yet"; '' when there is
 * nothing to say.
 */
export function ticketText(tickets, prefixes = TICKET_PREFIXES) {
    return ticketParts(tickets, prefixes).map((p) => p.text).join(' · ');
}

/**
 * Where a release PR goes, as a person reads it: "CARE PRL1", "CARE PRD" —
 * from the server's `env` (PRD/PRL1 for those two branches), else its base.
 * @param {{env?: string, base?: string}} pr
 * @param {string} [kind]
 */
export function releaseLabel(pr, kind = 'CARE') {
    const env = norm(pr && (pr.env || pr.base));
    return env ? kind + ' ' + env : kind;
}

/**
 * One banner line per release PR: "CARE PRL1 · CHG0099006 · RMG0042006", then
 * the PR it is read from. `parts` are ticketParts (empty when the PR could not
 * be read), so the label still shows.
 * @returns {{label: string, parts: object[], number: number, url: string, head: string, base: string}}
 */
export function releaseLine(pr, kind = 'CARE') {
    const p = pr && typeof pr === 'object' ? pr : {};
    return { label: releaseLabel(p, kind), parts: ticketParts(p.tickets), number: p.number,
             url: norm(p.url), head: norm(p.head), base: norm(p.base) };
}

/**
 * The banner title for the CARE releases in flight: "CARE PRL1 #102 · CARE PRD #101"
 * plus " · adds blocked" while the release guard holds; "CARE: no release PR open"
 * when there is none.
 */
export function releaseTitle(prs, blocked, kind = 'CARE') {
    const list = Array.isArray(prs) ? prs.filter(p => p && p.number) : [];
    if (!list.length) return kind + ': no release PR open';
    return list.map(p => releaseLabel(p, kind) + ' #' + p.number).join(' · ') + (blocked ? ' · adds blocked' : '');
}
