import { escapeHtml as esc } from '../core/format.js';
import {
    batchHeadline, budgetLine, confirmQuestion, groupRows, imagesFor, isDone, previewHeadline, progressLine,
} from '../core/rerun.js';
import { rerunBatch, rerunCandidates, rerunPreview, rerunStart } from '../api.js';
import { getThreadId } from '../state.js';
import { FIELD_CLASS, lockToSignedIn, opening, withDismiss } from './common.js';

// ---- Re-run a job -------------------------------------------------------------
// Pick any job (or step) name and the queued images; the preview reads each
// distinct build run once and answers every image; Yes spends the single-use
// plan and the batch runs on the server (it outlives this tab); the card
// follows it, and the bell links back here (portal:rerun:<id>).
//
// Kept SHORT on purpose — a batch is often 10–50 images: images are chips,
// answers are grouped (core/rerun.js groupRows) so each reason is said once,
// what needs nothing from the person folds away, and the activity log is
// collapsed. design/RERUN_JOB.md.

// Tints and dots are .rr-* rules in the page's style block (app_fastapi.py).
const TONES = new Set(['ok', 'info', 'warn', 'bad', 'muted']);
const tone = (t) => (TONES.has(t) ? t : 'muted');
const POLL_MS = 3000;
const CHIPS_SHOWN = 10;

const btnPri = 'px-3 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs disabled:opacity-50';
const btnSec = 'px-3 py-1 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-xs';

function safeUrl(url) {
    return url && (url.startsWith('https://') || url.startsWith('http://')) ? url : '';
}

// One image as a small chip; linked to its run (or the re-run's job) when known.
function chip(row, t) {
    const url = safeUrl(row.job_url || row.run_url);
    const cls = 'rr-chip rr-' + tone(t);
    const title = esc(row.detail || row.artifact) + (row.new_attempt ? ' · attempt ' + esc(row.new_attempt) : '') +
        (row.queue ? ' — ' + esc(row.queue) : '');
    return url
        ? '<a href="' + esc(url) + '" target="_blank" rel="noopener" title="' + title + '" class="' + cls +
          ' hover:underline">' + esc(row.artifact) + '</a>'
        : '<span title="' + title + '" class="' + cls + '">' + esc(row.artifact) + '</span>';
}

function chips(rows, t) {
    const head = rows.slice(0, CHIPS_SHOWN).map(r => chip(r, t)).join(' ');
    const rest = rows.slice(CHIPS_SHOWN);
    return '<div class="flex flex-wrap gap-1 mt-1">' + head +
        (rest.length ? '<details class="contents"><summary class="text-[11px] text-sky-400 cursor-pointer ' +
            'px-1">+' + rest.length + ' more</summary>' + rest.map(r => chip(r, t)).join(' ') + '</details>' : '') +
        '</div>';
}

// A group: one header line (dot, label, count), then each reason once with its images.
function groupHtml(g) {
    const body = g.reasons.map(r => (r.text ? '<div class="text-[11px] text-slate-400 mt-1">' + esc(r.text) + '</div>' : '') +
        chips(r.rows, g.tone)).join('');
    const head = '<span class="rr-dot ' + tone(g.tone) + '"></span>' +
        '<span class="text-xs text-slate-200">' + esc(g.label) + '</span>' +
        '<span class="text-[11px] text-slate-500">' + g.count + '</span>';
    if (g.quiet) {
        return '<details class="py-1.5 border-t border-slate-800"><summary class="flex items-center gap-2 cursor-pointer">' + head + '<span class="text-[11px] text-sky-400">show</span></summary>' + body + '</details>';
    }
    return '<div class="py-1.5 border-t border-slate-800"><div class="flex items-center gap-2">' + head + '</div>' + body + '</div>';
}

function card() {
    const wrap = document.createElement('div');
    wrap.className = 'message bot interrupt-box rounded-2xl p-4 text-sm queue-table-card rr-card';
    return wrap;
}

const title = (text, spin) => '<div class="font-semibold text-emerald-300 pr-6 flex items-center gap-2">' +
    '<i class="fa-solid fa-rotate-right' + (spin ? ' fa-spin' : '') + '"></i><span>' + esc(text) + '</span></div>';

/** @param {{job?: string, images?: string[]}} [prefill] */
export async function showRerunJob(prefill) {
    const _ph = opening('re-run a job');
    const chat = document.getElementById('chat');
    const wrap = card();
    let cand;
    try { cand = await rerunCandidates(); } catch (e) { cand = { ok: false, error: String((e && e.message) || e) }; }
    _ph.replaceWith(wrap);
    withDismiss(wrap);
    if (!cand || cand.ok === false) {
        wrap.innerHTML = title('Re-run a job') + '<div class="text-xs text-amber-400 mt-2">' +
            esc((cand && cand.error) || 'The release queue is unavailable.') + '</div>';
        return;
    }
    const usable = (cand.images || []).filter(i => i.run_url);
    const picked = new Set((prefill && prefill.images) || []);
    if (!picked.size) usable.forEach(i => { if (i.allowed_failures) picked.add(i.artifact); });   // open controls: the usual case
    const openCount = usable.filter(i => i.allowed_failures).length;
    wrap.innerHTML = title('Re-run a job') +
        '<div class="text-[11px] text-slate-500 mb-2">Any job or step name — e.g. <span class="font-mono">sdlc-peer-review</span>, ' +
        '<span class="font-mono">RCTLDEF0001691</span> or just <span class="font-mono">1691</span>. Nothing runs until you say yes.</div>' +
        '<div class="flex gap-2 items-center flex-wrap">' +
        '<input data-rr="job" aria-label="Job or step" class="' + FIELD_CLASS + ' rr-input font-mono" placeholder="job or step" ' +
        'value="' + esc((prefill && prefill.job) || '') + '">' +
        '<label class="text-[11px] text-slate-400 flex items-center gap-1"><input type="checkbox" data-rr="passed">also passed jobs</label>' +
        '</div>' +
        (usable.length
            ? '<div class="flex items-center gap-2 text-[11px] text-slate-500 mt-2"><span data-rr="count"></span>' +
              '<span class="flex-1"></span>' +
              (openCount ? '<button type="button" data-rr="sel-open" class="text-sky-400 hover:underline">with a control open (' +
                  openCount + ')</button>·' : '') +
              '<button type="button" data-rr="sel-all" class="text-sky-400 hover:underline">all</button>·' +
              '<button type="button" data-rr="sel-none" class="text-sky-400 hover:underline">none</button></div>' +
              '<div data-rr="list" class="rr-list flex flex-wrap gap-1 mt-1">' +
              usable.map((i, n) =>
                  '<label title="' + esc((i.allowed_failures ? i.allowed_failures + ' open · ' : '') + 'run ' + (i.run_id || '')) +
                  '" class="rr-pick relative cursor-pointer select-none"><input type="checkbox" data-img="' + n + '"' +
                  (picked.has(i.artifact) ? ' checked' : '') + '><span class="rr-chip">' +
                  (i.allowed_failures ? '<span class="rr-dot warn"></span>' : '') +
                  esc(i.artifact) + '</span></label>').join('') + '</div>'
            : '<div class="text-xs text-slate-500 mt-2">Nothing queued has a build run to re-run.</div>') +
        '<div class="flex gap-2 items-center flex-wrap mt-2">' +
        '<input data-rr="email" type="email" aria-label="Your email" class="' + FIELD_CLASS + ' rr-input" placeholder="your email (for the bell)">' +
        (usable.length ? '<button type="button" data-rr="preview" class="' + btnPri + '">Preview</button>' : '') +
        '</div><div data-rr="out"></div>';
    chat.scrollTop = chat.scrollHeight;
    const email = wrap.querySelector('[data-rr="email"]');
    lockToSignedIn(email).catch(() => null);
    const boxes = [...wrap.querySelectorAll('[data-img]')];
    const count = wrap.querySelector('[data-rr="count"]');
    const recount = () => { if (count) count.textContent = boxes.filter(b => b.checked).length + ' of ' + usable.length + ' queued images'; };
    const pick = (fn) => { boxes.forEach(b => { b.checked = fn(usable[+b.dataset.img]); }); recount(); };
    boxes.forEach(b => b.addEventListener('change', recount));
    const on = (sel, fn) => { const el = wrap.querySelector(sel); if (el) el.addEventListener('click', fn); };
    on('[data-rr="sel-all"]', () => pick(() => true));
    on('[data-rr="sel-none"]', () => pick(() => false));
    on('[data-rr="sel-open"]', () => pick(i => !!i.allowed_failures));
    recount();
    const btn = wrap.querySelector('[data-rr="preview"]');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        const out = wrap.querySelector('[data-rr="out"]');
        const job = wrap.querySelector('[data-rr="job"]').value.trim();
        const chosen = boxes.filter(b => b.checked).map(b => usable[+b.dataset.img].artifact);
        if (!job) { out.innerHTML = '<div class="text-xs text-amber-400 mt-2">Type the job or step to re-run.</div>'; return; }
        if (!chosen.length) { out.innerHTML = '<div class="text-xs text-amber-400 mt-2">Pick at least one image.</div>'; return; }
        btn.disabled = true;
        out.innerHTML = '<div class="text-[11px] text-slate-500 mt-2"><span class="dots"><span></span><span></span>' +
            '<span></span></span> Reading the build runs of ' + chosen.length + ' image' + (chosen.length === 1 ? '' : 's') + '…</div>';
        let plan;
        try {
            plan = await rerunPreview({ job, images: chosen, include_passed: wrap.querySelector('[data-rr="passed"]').checked,
                                        thread_id: getThreadId() });
        } catch (e) { plan = { ok: false, error: String((e && e.message) || e) }; }
        btn.disabled = false;
        renderPreview(out, plan, email);
        chat.scrollTop = chat.scrollHeight;
    });
}

function renderPreview(out, plan, email) {
    if (!plan || plan.ok === false) {
        out.innerHTML = '<div class="text-xs text-red-400 mt-2">' + esc((plan && plan.error) || 'The preview failed.') + '</div>';
        return;
    }
    const q = confirmQuestion(plan);
    const budget = budgetLine(plan.budget, (plan.runs || []).length);
    out.innerHTML =
        '<div class="border-t border-slate-700 mt-2 pt-2">' +
        '<div class="text-xs text-slate-300 mb-1"><span class="text-slate-500">Preview · read-only ·</span> ' +
        esc(previewHeadline(plan)) + '</div>' +
        groupRows(plan.rows, 'preview').map(groupHtml).join('') +
        (q ? '<div class="flex gap-2 items-center flex-wrap mt-2 pt-2 border-t border-slate-800">' +
            '<span class="text-xs text-slate-100 font-medium">' + esc(q) + '</span>' +
            '<button type="button" data-rr="yes" class="' + btnPri + '">Yes</button>' +
            '<button type="button" data-rr="no" class="' + btnSec + '">No</button></div>' +
            '<div class="rr-small text-slate-500 mt-1">A new attempt on the same commit; jobs that depend on it ' +
            're-run too. ' + esc(budget) + '</div>'
            : '<div class="text-xs text-slate-400 mt-2">Nothing to re-run.</div>') +
        '</div>';
    const yes = out.querySelector('[data-rr="yes"]');
    if (!yes) return;
    const line = yes.parentElement;
    out.querySelector('[data-rr="no"]').addEventListener('click', () => {
        line.innerHTML = '<span class="text-xs text-slate-400">Not re-run — nothing was sent to GitHub.</span>';
    });
    yes.addEventListener('click', async () => {
        yes.disabled = true;
        let res;
        try {
            res = await rerunStart({ plan_id: plan.plan_id, requested_by: (email && email.value || '').trim(),
                                     thread_id: getThreadId() });
        } catch (e) { res = { ok: false, error: String((e && e.message) || e) }; }
        if (!res || res.ok === false) {
            line.innerHTML = '<span class="text-xs text-red-400">' + esc((res && res.error) || 'Could not start.') + '</span>';
            return;
        }
        // The batch replaces the preview: one card, not a preview and a result.
        follow(out, res.batch_id, res.batch);
    });
}

function batchHtml(b) {
    const done = isDone(b);
    const failed = imagesFor(b, 'failed_again');
    const notStarted = imagesFor(b, 'not_started');
    const lines = b.lines || [];
    return '<div class="border-t border-slate-700 mt-2 pt-2">' +
        '<div class="flex items-center gap-2 flex-wrap text-xs">' +
        '<i class="fa-solid fa-rotate-right text-emerald-300' + (done ? '' : ' fa-spin') + '"></i>' +
        '<span class="text-slate-100 font-medium">' + esc(batchHeadline(b)) + '</span>' +
        (done ? '' : '<span class="text-slate-500">· ' + esc(progressLine(b)) + '</span>') + '</div>' +
        (b.stopped || b.stopped_starts ? '<div class="text-[11px] text-amber-400 mt-1">' + esc(b.stopped || b.stopped_starts) + '</div>' : '') +
        groupRows(b.rows, 'result').map(groupHtml).join('') +
        (done && (failed.length || notStarted.length) ? '<div class="flex gap-2 items-center mt-2 pt-2 border-t border-slate-800 flex-wrap">' +
            (failed.length ? '<button type="button" data-rr="again" class="' + btnPri + '">Re-run the ' + failed.length +
                ' that failed again</button>' : '') +
            (notStarted.length ? '<button type="button" data-rr="rest" class="' + btnSec + '">Start the ' + notStarted.length +
                ' not started</button>' : '') +
            '<span class="rr-small text-slate-500">a new preview and a new yes</span></div>' : '') +
        (lines.length ? '<details class="mt-2"><summary class="text-[11px] text-slate-500 cursor-pointer">Activity · ' +
            lines.length + ' line' + (lines.length === 1 ? '' : 's') + '</summary><div class="font-mono text-[11px] ' +
            'text-slate-400 bg-slate-900/60 rounded-lg p-3 mt-1 rr-log">' +
            lines.map(l => '<div>' + esc(l.text) + '</div>').join('') + '</div></details>' : '') +
        '</div>';
}

function follow(el, id, first) {
    const paint = (b) => {
        const open = el.querySelector('details[open]') !== null;     // keep the log open across repaints
        el.innerHTML = batchHtml(b);
        if (open) el.querySelectorAll('details').forEach(d => { if (d.querySelector('.font-mono')) d.open = true; });
        const again = el.querySelector('[data-rr="again"]');
        if (again) again.addEventListener('click', () => showRerunJob({ job: b.job, images: imagesFor(b, 'failed_again') }));
        const rest = el.querySelector('[data-rr="rest"]');
        if (rest) rest.addEventListener('click', () => showRerunJob({ job: b.job, images: imagesFor(b, 'not_started') }));
    };
    if (first) paint(first);
    const tick = async () => {
        if (!el.isConnected) return;
        let res;
        try { res = await rerunBatch(id); } catch (e) { res = null; }
        if (res && res.ok && res.batch) {
            paint(res.batch);
            if (isDone(res.batch)) return;
        } else if (res && res.ok === false) {
            el.innerHTML = '<div class="text-xs text-red-400 mt-2">' + esc(res.error || 'The re-run could not be read.') + '</div>';
            return;
        }
        setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, first ? POLL_MS : 0);
}

/** The result card for one batch — where the bell's portal:rerun:<id> lands. */
export function showRerunBatch(id) {
    const chat = document.getElementById('chat');
    const wrap = card();
    wrap.innerHTML = title('Re-run');
    const live = document.createElement('div');
    wrap.appendChild(live);
    chat.appendChild(wrap);
    withDismiss(wrap);
    follow(live, id, null);
    chat.scrollTop = chat.scrollHeight;
}
