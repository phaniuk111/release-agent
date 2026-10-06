// Helpers every form card shares. The screens themselves are one module each
// in this folder; core/ holds their rules and ../api.js every backend call.
import { escapeHtml as esc } from '../core/format.js';
import { whoami } from '../api.js';

// When the gateway has signed the person in, the server records THAT email
// whatever a field says — so the field shows it and stops being editable,
// rather than inviting a value that would be silently replaced.
export async function lockToSignedIn(input) {
    const who = await whoami();
    if (!who || !who.signed_in || !who.email) return null;
    input.value = who.email;
    input.readOnly = true;
    input.dataset.signedIn = '1';
    input.title = 'Signed in through the gateway — recorded as you';
    input.classList.add('opacity-70');
    return who;
}

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

// ---- the labelled field every form card is built from ----------------------
// One builder, one class string. Four forms used to carry their own copy of
// this (plus two inline ones), and the copies had already drifted apart — the
// queue rows were rendered a padding step smaller than every other field.
//
// spec: {label?, id?, type?, tag?, placeholder?, value?, default?, options?,
//        rows?, list?, title?, className?, boxClass?}
//   options  -> a <select>: a workflow `choice` input, where GitHub refuses any
//               value outside its options:, so free text there only earns a
//               refusal after the developer has already confirmed.
//   tag      -> 'textarea'; anything else is an <input> of `type` (default text).
//   className REPLACES the default 'w-full' sizing — row fields size themselves
//               (flex-1 / w-24) and would fight a full-width rule.
export const FIELD_CLASS = 'bg-slate-900 border border-slate-700 rounded-lg ' +
    'px-3 py-1.5 text-xs text-white focus:outline-none';

/** The control alone, for rows that carry their own header instead of a label. */
export function fieldControl(spec) {
    const opts = spec.options || [];
    const el = document.createElement(opts.length ? 'select' : (spec.tag || 'input'));
    if (spec.id) el.id = spec.id;
    el.className = FIELD_CLASS + ' ' + (spec.className == null ? 'w-full' : spec.className);
    if (opts.length) {
        // A blank first entry, so the field starts on nothing rather than on a
        // value nobody picked.
        const blank = document.createElement('option');
        blank.value = '';
        blank.textContent = 'select ' + String(spec.label || '').toLowerCase() + '…';
        el.appendChild(blank);
        opts.forEach(o => {
            const opt = document.createElement('option');
            opt.value = o; opt.textContent = o;
            el.appendChild(opt);
        });
        if (spec.default != null && opts.indexOf(spec.default) !== -1) el.value = spec.default;
    } else {
        if (el.tagName === 'INPUT') el.type = spec.type || 'text';
        if (spec.rows) el.rows = spec.rows;
        if (spec.placeholder) el.placeholder = spec.placeholder;
        if (spec.default != null) el.value = spec.default;
    }
    if (spec.value != null) el.value = spec.value;
    if (spec.list) el.setAttribute('list', spec.list);
    if (spec.title) el.title = spec.title;
    if (spec.spellcheck === false) el.spellcheck = false;
    return el;
}

/** A <label> above its control, boxed, appended to the caller's grid. */
export function labeledField(parent, spec) {
    const el = fieldControl(spec);
    const box = document.createElement('div');
    if (spec.boxClass) box.className = spec.boxClass;
    if (spec.label) {
        const l = document.createElement('label');
        l.className = 'text-[11px] text-slate-400 block mb-0.5';
        l.textContent = spec.label;
        box.appendChild(l);
    }
    box.appendChild(el);
    if (parent) parent.appendChild(box);
    return el;
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
