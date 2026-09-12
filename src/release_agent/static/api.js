// Every call the UI makes to the backend, in one place — the contract a React
// port reimplements (the Backstage plugin's api.ts talks to the same routes).
// Screens never call fetch() themselves.
//
// One result shape: a network failure throws (as fetch does); an HTTP error
// comes back as {ok: false, status, error, ...body}, so a caller handles a
// refused request and a crashed one the same way. The request/response models
// live server-side in app_fastapi.py (pydantic) and in /openapi.json.
import { API_BASE } from './state.js';

// Context fetches must NEVER block a form from opening: a slow or broken
// endpoint (BigQuery not provisioned, GitHub slow through a TLS-inspecting
// proxy) used to leave a pill looking dead. 15s, because a COLD BigQuery query
// plus GitHub reads regularly exceeded 5s — and a form opened with an empty
// queue quietly hides "Already queued".
export const CONTEXT_TIMEOUT_MS = 15000;

async function call(method, path, body, signal) {
    const r = await fetch(API_BASE + path, {
        method,
        signal,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    const obj = data && typeof data === 'object' && !Array.isArray(data) ? data : null;
    if (!r.ok) {
        const reason = obj && (obj.error || obj.detail);
        return Object.assign({}, obj || {}, {
            ok: false, status: r.status,
            error: reason ? String(typeof reason === 'string' ? reason : JSON.stringify(reason)) : 'HTTP ' + r.status,
        });
    }
    if (data === null) return { ok: false, status: r.status, error: 'the answer was not JSON — a proxy or login page?' };
    return data;
}

const get = (path, signal) => call('GET', path, undefined, signal);
const post = (path, body) => call('POST', path, body);
const query = (params) => new URLSearchParams(params).toString();

/**
 * Context for opening a form: never throws, never waits past the timeout.
 * Returns {...fallback, ...answer}; on failure {...fallback, _ctxError}.
 */
export async function getContext(path, fallback, timeoutMs = CONTEXT_TIMEOUT_MS) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
        const data = await get(path, ctl.signal);
        if (data.ok === false && data.status) return Object.assign({}, fallback, { _ctxError: 'HTTP ' + data.status });
        return Object.assign({}, fallback, data);
    } catch (e) {
        return Object.assign({}, fallback, {
            _ctxError: e && e.name === 'AbortError' ? 'timed out' : String((e && e.message) || e),
        });
    } finally {
        clearTimeout(timer);
    }
}

// ---- release queue ----------------------------------------------------------
export const QUEUE_PATH = '/api/release-queue';
export const getQueue = () => get(QUEUE_PATH);
export const queueBatch = (body) => post('/api/release-queue/batch', body);
/** @param {{artifact_name: string, artifact_version?: string, requested_by: string}} body */
export const withdrawFromQueue = (body) => post('/api/release-queue/withdraw', body);

// ---- releases -----------------------------------------------------------------
export const releaseDefaults = (body) => post('/api/release-defaults', body);
export const releaseDraft = (body) => post('/api/release-draft', body);
export const releaseStatus = (fresh) => get('/api/release-status' + (fresh ? '?fresh=1' : ''));
export const releaseInsights = (params) => get('/api/release-insights?' + query(params));

// ---- deploy forms (context paths, opened through getContext) -------------------
export const dfTemplatePath = (env) => '/api/df-template?' + query({ env });
export const deployTemplatePath = (params) => '/api/deploy-template?' + query(params);

// ---- page chrome -----------------------------------------------------------------
export const consoleLinks = () => get('/api/console-links');

// ---- GitHub session (per chat thread) ---------------------------------------------
export const sessionStatus = (threadId) => get('/api/session/status?' + query({ thread_id: threadId }));
export const sessionConnect = (threadId, patToken) =>
    post('/api/session/connect', { thread_id: threadId, pat_token: patToken });
export const sessionDisconnect = (threadId) => post('/api/session/disconnect', { thread_id: threadId });

// ---- chat -------------------------------------------------------------------------
// A server-sent-event stream, so the raw Response comes back for the reader.
export function openChat(message, threadId) {
    return fetch(API_BASE + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, thread_id: threadId }),
    });
}
