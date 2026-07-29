// Entry point: wires page load, keyboard shortcuts, and exposes the functions
// the HTML's inline onclick handlers need (ES modules don't create globals).
import { getThreadId } from './state.js';
import { addMessage, sendMessage, sendConfirmation, sendApproval, newThread } from './chat.js';
import { renderConnectionStatus, refreshConnectionStatus, showConnectForm } from './connect.js';
import { showCapabilities, openPalette } from './palette.js';
import { toggleInsights, renderInsights } from './insights.js';
import { loadReleaseStatus, toggleBannerDetail, startBannerAgeTicker, showBannerIdle } from './status.js';

// Inline onclick handlers in the served HTML (and interrupt-box templates).
Object.assign(window, {
    sendMessage, sendConfirmation, sendApproval, newThread,
    showConnectForm, showCapabilities, openPalette,
    toggleInsights, loadReleaseStatus, toggleBannerDetail,
    renderConnectionStatus,
});

document.getElementById('thread-label').textContent = getThreadId();

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
    // Keep it fresh so a release raised in another session shows up here.
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
