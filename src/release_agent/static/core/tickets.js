// How a PR's CHG / RMG tickets read on the banner — the one rule both UIs share.
// Pure: no DOM, no fetch. The server's `tickets` object (tools/tickets.py) is
// one entry per prefix, `{chg: {id, state, source, url} | null, rmg: ...}`, or
// `{error}` when the PR could not be read.
//
// Each prefix becomes one part: the ticket with its state ("CHG-12345 approved",
// a link to where it was said) or, when the PR names none yet, a grey
// "CHG pending" — the ticket a release still needs is as much a fact as the one
// it has. An error, or no tickets object at all, is no parts: the banner then
// shows the PR as it did before, rather than a wrong "pending".

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
 * @returns {{prefix: string, id: string, state: string, url: string, pending: boolean, text: string}[]}
 */
export function ticketParts(tickets, prefixes = TICKET_PREFIXES) {
    if (!tickets || typeof tickets !== 'object' || tickets.error) return [];
    return prefixes.map((prefix) => {
        const hit = tickets[prefix.toLowerCase()];
        const id = norm(hit && hit.id);
        if (!id) {
            return { prefix, id: '', state: '', url: '', pending: true, text: prefix + ' pending' };
        }
        const state = norm(hit.state);
        return {
            prefix, id, state, url: norm(hit.url), pending: false,
            text: state ? id + ' ' + state : id,
        };
    });
}

/**
 * The parts as one line: "CHG-12345 · RMG-678 approved", "CHG pending · RMG pending"; '' when there is
 * nothing to say.
 */
export function ticketText(tickets, prefixes = TICKET_PREFIXES) {
    return ticketParts(tickets, prefixes).map((p) => p.text).join(' · ');
}
