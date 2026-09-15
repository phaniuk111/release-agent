// Fenced code blocks (```lang … ```) in chat text. Pure — tested in
// tests/js/fences.test.mjs.
//
// The chat renderer only knew inline `code`, so a fenced block (every deploy
// and release preview carries one) was paired backtick-by-backtick with the
// text around it: the JSON rendered as one long inline span and stray
// backticks leaked into "Reply `CONFIRM-…` to confirm". Blocks are lifted out
// FIRST, so no inline rule ever sees their backticks, and put back last.

/**
 * @param {string} text
 * @returns {{text: string, blocks: {lang: string, code: string}[]}}
 *   text with each complete block replaced by CODESLOT<n>ENDCODE on its own
 *   line. An unterminated block (still streaming) is left as text.
 */
export function liftFences(text) {
    const blocks = [];
    let out = '', idx = 0;
    const src = String(text || '');
    while (true) {
        const start = src.indexOf('```', idx);
        if (start === -1) { out += src.slice(idx); break; }
        const nl = src.indexOf('\n', start);
        const close = nl === -1 ? -1 : src.indexOf('```', nl);
        if (close === -1) { out += src.slice(idx); break; }
        out += src.slice(idx, start);
        const lang = src.slice(start + 3, nl).trim();
        let code = src.slice(nl + 1, close);
        if (code.endsWith('\n')) code = code.slice(0, -1);
        blocks.push({ lang, code });
        out += '\nCODESLOT' + (blocks.length - 1) + 'ENDCODE\n';
        idx = close + 3;
    }
    return { text: out, blocks };
}
