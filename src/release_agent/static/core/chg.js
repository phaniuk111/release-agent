// The change-request rules both UIs must agree on: where a prose field's text
// came from (the chip beside it), when an automatic value may replace what a
// field holds, and when two sets of artifact lines are the same release. Pure:
// no DOM, no fetch — the Node tests import it directly and a React port (the
// Backstage plugin included) can use it as is.
//
// Each prose field has ONE automatic writer at a time: the AI draft while it
// was made for the items the form holds, otherwise the standard wording from
// the defaults. Neither ever replaces text the person typed, so the chip is
// decided by comparing the field with the last automatic value put there.

/** The release file's prose keys, in the file's own order. */
export const PROSE_FIELDS = ['change_description', 'change_reason', 'associated_risk',
    'consequence', 'user_service_impact'];

/** A DF release drafts its summary too (a CARE mono summary is the release name). */
export const DF_DRAFT_FIELDS = ['change_summary', ...PROSE_FIELDS];

/** What each chip says, and its tooltip. */
export const CHIPS = {
    ai: { label: 'AI draft', title: 'Summarised by the model from the developers\' queue entries — review it' },
    team: { label: 'Team wording', title: 'The team\'s standard wording, built from the queued facts' },
    edited: { label: 'Edited', title: 'Your text — Regenerate leaves it alone' },
    fallback: { label: 'Fallback', title: 'The draft for this field came back empty or too long, ' +
        'so the standard wording is used' },
};

const norm = (v) => String(v == null ? '' : v).trim();

/**
 * May an automatic value replace what the field holds? Yes when the field is empty or still holds the last
 * automatic value (auto.value, compared trimmed) — i.e. the person has not edited it.
 * @param {string} value  the field's current text
 * @param {{value: string, source: string}|null|undefined} auto  the last automatic write, if any
 */
export function canReplace(value, auto) {
    const now = norm(value);
    return !now || (!!auto && norm(auto.value) === now);
}

/**
 * The chip for a prose field:
 *  ''         — the field is empty (no chip)
 *  'edited'   — it holds text no automatic writer put there
 *  auto.source ('ai' | 'team' | 'fallback') — it still holds the last automatic value (compared trimmed);
 *               an unknown source reads as 'ai', the label that asks for review
 * @returns {''|'ai'|'team'|'fallback'|'edited'}
 */
export function chipState(value, auto) {
    const now = norm(value);
    if (!now) return '';
    if (!auto || norm(auto.value) !== now) return 'edited';
    return auto.source === 'team' || auto.source === 'fallback' ? auto.source : 'ai';
}

/**
 * One key per set of artifact lines — a draft made for one key is stale for any other. Each line counts by
 * its last path segment (name:version), so a full registry URL and its bare name:version are the same item;
 * blank lines and trailing slashes are ignored, duplicates count once, and order does not matter.
 * @param {string[]} lines
 */
export function itemsKey(lines) {
    const seen = new Set();
    (lines || []).forEach(line => {
        let s = norm(line);
        while (s.endsWith('/')) s = s.slice(0, -1);
        const last = s.split('/').pop().trim();
        if (last) seen.add(last);
    });
    return Array.from(seen).sort().join('\n');
}

/** The CARE form's header in mono mode (plain text — the screen escapes it). */
export function monoReleaseNote(file, repo) {
    return 'One file — ' + (norm(file) || 'the release file') + ' in ' +
        (norm(repo) || 'the release repo (CARE_RELEASE_REPO is not set)') +
        '. The portal raises a PR for you to review and merge; ' +
        'you\'ll see the exact change before anything is pushed.';
}
