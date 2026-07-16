// Shared session state: the chat thread id and the API base path.
// Base path so the UI works at "/" AND under a shared-domain path prefix
// (e.g. /release-copilot). Derived from where this page is served.
export const API_BASE = (function () {        // strip trailing slashes (regex-free)
    let p = window.location.pathname;
    while (p.endsWith('/')) p = p.slice(0, -1);
    return p;
})();

let threadId = localStorage.getItem('thread_id') || 'fastapi-' + Math.random().toString(36).slice(2, 10);
localStorage.setItem('thread_id', threadId);

export function getThreadId() {
    return threadId;
}

export function rotateThreadId() {
    threadId = 'fastapi-' + Math.random().toString(36).slice(2, 10);
    localStorage.setItem('thread_id', threadId);
    return threadId;
}
