import { escapeHtml as esc } from '../core/format.js';
import {
    CAUSE_PROMPT, MAX_CAUSE, VERDICTS, asksForCause, feedbackPayload, thanksText,
} from '../core/support_feedback.js';
import { supportFeedback } from '../api.js';

// ---- "Was this right?" under an investigation's answer ----------------------------
// chat.js calls renderInvestigationFeedback(messageEl, data) when the stream says
// {"type":"investigation","data":{business_date, incident_id, title, model,
// model_calls, seconds}} — a CONTRACT, keep the name and signature. There is no
// model in here: it shows with LLM_ENABLED=false too. Who answered is decided by
// the server (the verified caller); nothing but the answer is sent from here.
//
// The cause is the person's own words, so every value goes through esc().

const VERDICT_CLASS = {
    right: 'text-emerald-300',
    direction: 'text-amber-300',
    wrong: 'text-red-300',
};
const BUTTON_CLASS = 'rounded border border-slate-700 bg-slate-800 px-2 py-0.5 text-[11px] font-semibold hover:bg-slate-600 whitespace-nowrap';

function buttonsHtml() {
    return VERDICTS.map(v =>
        '<button type="button" data-fb-verdict="' + esc(v.key) + '" class="' + BUTTON_CLASS + ' ' +
        (VERDICT_CLASS[v.key] || 'text-slate-200') + '">' + esc(v.label) + '</button>').join('');
}

function showThanks(row, verdict) {
    row.innerHTML = '<span class="text-slate-400"><i class="fa-solid fa-check mr-1"></i>' +
        esc(thanksText(verdict)) + '</span>';
}

/** Appends the feedback row under the answer in `messageEl`; calling it again on
 *  the same element adds nothing. */
export function renderInvestigationFeedback(messageEl, data) {
    if (!messageEl || typeof messageEl.querySelector !== 'function') return;
    if (messageEl.querySelector('[data-feedback]')) return;

    const row = document.createElement('div');
    row.setAttribute('data-feedback', '1');
    row.className = 'flex items-center gap-2 flex-wrap mt-2 text-[11px]';
    row.innerHTML = '<span class="text-slate-400">Was this right?</span>' + buttonsHtml() +
        '<span data-fb-cause class="flex items-center gap-2 w-full" style="display:none">' +
        '<input type="text" data-fb-text maxlength="' + MAX_CAUSE + '" placeholder="' + esc(CAUSE_PROMPT) + '" ' +
        'aria-label="' + esc(CAUSE_PROMPT) + '" ' +
        'class="flex-1 min-w-0 bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5 text-[11px] text-slate-200">' +
        '<button type="button" data-fb-save class="' + BUTTON_CLASS + ' text-sky-400">Save</button></span>' +
        '<span data-fb-error class="w-full text-amber-400" style="display:none"></span>';
    messageEl.appendChild(row);

    const causeBox = row.querySelector('[data-fb-cause]');
    const input = row.querySelector('[data-fb-text]');
    const errorEl = row.querySelector('[data-fb-error]');
    const controls = () => row.querySelectorAll('button');
    let picked = '';
    let sending = false;

    async function send(verdict) {
        if (sending) return;
        sending = true;
        controls().forEach(b => { b.disabled = true; });
        errorEl.style.display = 'none';
        let res;
        try {
            res = await supportFeedback(feedbackPayload(data, verdict, input.value));
        } catch (e) {
            res = { ok: false, error: String((e && e.message) || e) };
        }
        sending = false;
        if (res && res.ok) {
            showThanks(row, verdict);
            return;
        }
        controls().forEach(b => { b.disabled = false; });
        const why = (res && res.error) || 'no answer';
        errorEl.innerHTML = esc(why) + (res && res.hint ? ' ' + esc(res.hint) : '');
        errorEl.style.display = '';
        if (res && res.disabled) controls().forEach(b => { b.style.display = 'none'; });
    }

    row.querySelectorAll('button[data-fb-verdict]').forEach(btn => {
        btn.addEventListener('click', () => {
            picked = btn.dataset.fbVerdict;
            if (asksForCause(picked)) {
                causeBox.style.display = '';
                input.focus();
            } else {
                causeBox.style.display = 'none';
                send(picked);
            }
        });
    });
    row.querySelector('[data-fb-save]').addEventListener('click', () => { if (picked) send(picked); });
    input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' && picked) { ev.preventDefault(); send(picked); }
    });

    const chat = document.getElementById('chat');
    if (chat) chat.scrollTop = chat.scrollHeight;
}
