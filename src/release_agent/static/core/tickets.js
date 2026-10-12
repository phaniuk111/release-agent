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
