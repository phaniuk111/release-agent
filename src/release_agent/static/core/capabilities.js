// Which pills this person sees — shared with the Backstage port, so pure and
// tested in tests/js/capabilities.test.mjs. The server decides (features.py)
// and bakes it into the page as window.PORTAL_UI; this only applies it.

/**
 * @param {{name: string}[]} groups
 * @param {{group: string}[]} capabilities
 * @param {{hiddenGroups?: string[], previewGroups?: string[]}} ui
 * @returns {{groups: {name: string, preview: boolean}[], capabilities: object[]}}
 */
export function visibleCapabilities(groups, capabilities, ui) {
    const hidden = new Set((ui && ui.hiddenGroups) || []);
    const preview = new Set((ui && ui.previewGroups) || []);
    return {
        groups: groups.filter(g => !hidden.has(g.name)).map(g => ({ ...g, preview: preview.has(g.name) })),
        // A pill that names a `feature` shows only when the server listed it for
        // this person (features.UI_FEATURES) — the same gate its routes apply.
        capabilities: capabilities.filter(c => !hidden.has(c.group) && (!c.feature || hasFeature(ui, c.feature))),
    };
}

/**
 * Whether the server listed a page-level feature (a bell, a strip, a pill)
 * for this person. Absent list = nothing of the kind is on.
 * @param {{features?: string[]}} ui
 * @param {string} name
 */
export function hasFeature(ui, name) {
    return !!(ui && Array.isArray(ui.features) && ui.features.indexOf(name) !== -1);
}

/**
 * Whether the portal has a model behind it (LLM_ENABLED). Anything that only a
 * model can answer — the chat's free text, "Ask why", AI drafting — is offered
 * only when this is true. A page without the flag predates it: a model is on.
 * @param {{llm?: boolean}} ui
 */
export function llmEnabled(ui) {
    return !ui || ui.llm !== false;
}
