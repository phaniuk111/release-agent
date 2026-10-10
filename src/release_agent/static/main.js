// Entry point: wires page load, keyboard shortcuts, and exposes the functions
// the HTML's inline onclick handlers need (ES modules don't create globals).
import { getThreadId } from './state.js';
import { addMessage, sendMessage, sendConfirmation, sendApproval, newThread } from './chat.js';
import { refreshConnectionStatus, showConnectForm } from './connect.js';
import { showCapabilities, openPalette } from './palette.js';
import { toggleInsights, renderInsights } from './insights.js';
import { loadReleaseStatus, toggleBannerDetail, startBannerAgeTicker, showBannerIdle, startAutoTriageStrip } from './status.js';
import { renderConsoleLinks } from './links.js';
import { llmEnabled } from './core/capabilities.js';
import { mountNotifyBell } from './notify.js';

// Inline onclick handlers in the served HTML (and interrupt-box templates).
// EXACTLY the names an onclick= names — a global nothing calls is a module
// boundary quietly given up. Everything else is imported where it is used.
Object.assign(window, {
    sendMessage, sendConfirmation, sendApproval, newThread,
    showConnectForm, openPalette,
    toggleInsights, loadReleaseStatus, toggleBannerDetail,
});

document.getElementById('thread-label').textContent = getThreadId();
// No model (LLM_ENABLED=false): the chat answers the pills and their commands
// only — say so where people type.
if (!llmEnabled(window.PORTAL_UI)) {
    document.getElementById('input').placeholder = 'Pick an action above, or type a command — / for the list';
}

window.onload = () => {
    const chat = document.getElementById('chat');
    if (chat.children.length === 0) {
        addMessage('bot', 'Welcome to the Dev Portal — deploys, releases and release insights in one place.');
        showCapabilities();
    }
    // Restore the Insights drawer if it was open last time.
    if (localStorage.getItem('insights_open') === '1') {
        document.getElementById('insights-panel').classList.remove('hidden');
        renderInsights();
    }
    refreshConnectionStatus();
    showBannerIdle();
    startBannerAgeTicker();
    // Absent unless the auto-triage feature is on for this person; one event-log
    // read a minute, no GitHub call.
    startAutoTriageStrip();
    // Static config read — unlike the release banner this costs no GitHub call,
    // so it can load on every page view.
    renderConsoleLinks();
    // The bell: only for a person the feature is on for (window.PORTAL_UI.features).
    mountNotifyBell();
};

// ⌘K / Ctrl+K anywhere, or "/" in an empty composer, opens the palette.
document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
});

// Enter key support ("/" in an empty composer opens the palette)
document.getElementById('input').addEventListener('keypress', function(e) {
    if (e.key === '/' && !e.target.value) { e.preventDefault(); openPalette(); return; }
    if (e.key === 'Enter') sendMessage();
});
