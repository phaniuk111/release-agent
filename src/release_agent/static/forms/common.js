// Helpers every form card shares. The screens themselves are one module each
// in this folder; core/ holds their rules and ../api.js every backend call.
import { escapeHtml as esc } from '../core/format.js';

// An accidentally-opened form is long (CARE Release especially) and used to
// leave the user scrolling past it. Every form card gets a ✕ in its corner, and
// Escape closes the most recently opened one. Closing sends nothing.
export function withDismiss(wrap) {
    wrap.classList.add('relative', 'dismissible-form');

    const close = () => {
        wrap.remove();
        document.removeEventListener('keydown', onKey);
    };
    const onKey = (e) => {
        if (e.key !== 'Escape') return;
        if (!document.body.contains(wrap)) { document.removeEventListener('keydown', onKey); return; }
        const palette = document.getElementById('palette-overlay');
        if (palette && !palette.classList.contains('hidden')) return;   // palette owns Esc while open
        const open = document.querySelectorAll('.dismissible-form');
        if (open.length && open[open.length - 1] !== wrap) return;      // only the newest closes
        close();
    };
    document.addEventListener('keydown', onKey);

    const x = document.createElement('button');
    x.type = 'button';
    x.title = 'Close this form (Esc) — nothing is sent';
    x.className = 'absolute top-2 right-3 text-slate-500 hover:text-red-400 text-sm leading-none';
    x.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    x.addEventListener('click', close);
    wrap.appendChild(x);
    return wrap;
}


// A click must feel instant even when the context endpoint takes seconds
// (BigQuery + GitHub reads). Show a placeholder bubble immediately; the form
// replaces it when ready.
export function opening(label) {
    const chat = document.getElementById('chat');
    const ph = document.createElement('div');
    ph.className = 'message bot rounded-2xl px-4 py-3 text-sm text-slate-500';
    ph.innerHTML = '<span class="dots"><span></span><span></span><span></span></span> Opening ' +
        esc(label) + '…';
    chat.appendChild(ph);
    chat.scrollTop = chat.scrollHeight;
    return ph;
}

// Small inline warning appended to a form when its context couldn't load.
export function ctxNote(ctx, what) {
    if (!ctx._ctxError) return null;
    const d = document.createElement('div');
    d.className = 'text-[11px] text-amber-400/90 mb-2';
    d.innerHTML = '<i class="fa-solid fa-triangle-exclamation mr-1"></i>Couldn\'t load ' +
        what + ' (' + esc(ctx._ctxError) + ') — the form still works; fields aren\'t pre-filled.';
    return d;
}
