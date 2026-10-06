// Where a deploy request can actually go. Pure (no DOM, no fetch) so both UIs —
// this portal and the Backstage port — answer a typed "deploy X:1 to prod" with
// the same sentence; tested in tests/js/deploy_routing.test.mjs.
//
// Everything ships through the intake queue → CARE/DF release → promote, so a
// single-chart deploy has exactly ONE destination: UAT. PROD is reached only by
// promoting a release file-set. The backend refuses a prod deploy as well — this
// rule is the courtesy that saves the round trip, never the gate.

/** The one sentence a prod deploy request is answered with. */
export const PROD_IS_RELEASE_ONLY =
    'PROD is reached through a release, not a single-chart deploy. ' +
    'Queue it (Add to next release), then raise the CARE or DF release, and promote it.';

/**
 * How a deploy request naming `env` is served.
 * @param {string} env environment named in the message ('uat', 'prod', 'prd', …)
 * @returns {{form: string|null, releaseOnly: boolean, message: string}|null}
 *   `form` is the deploy form to open; `releaseOnly` means answer with `message`
 *   instead. null when the text names no environment this portal deploys to.
 */
export function deployRoute(env) {
    const e = String(env || '').trim().toLowerCase();
    if (e === 'uat') return { form: 'uat', releaseOnly: false, message: '' };
    if (e === 'prod' || e === 'prd' || e === 'production') {
        return { form: null, releaseOnly: true, message: PROD_IS_RELEASE_ONLY };
    }
    return null;
}
