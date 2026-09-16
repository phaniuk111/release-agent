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
        capabilities: capabilities.filter(c => !hidden.has(c.group)),
    };
}
