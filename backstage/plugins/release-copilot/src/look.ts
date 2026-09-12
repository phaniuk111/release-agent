/**
 * The Dev Portal look, as values — the same colours, surfaces and shapes the
 * standalone portal uses (its page stylesheet in app_fastapi.py and Tailwind's
 * slate / emerald / teal scales). One source for both the app theme
 * (packages/app/src/modules/theme.tsx) and the plugin's own components.
 * Plain values, no dependencies, so any package can import them.
 */
export const DEV_PORTAL = {
  // Surfaces
  page: '#080d1a',
  glass: 'rgba(15, 23, 42, 0.55)', // cards and panels (slate-900 @55%)
  raised: 'rgba(30, 41, 59, 0.6)', // agent chat bubbles (slate-800 @60%)
  sunken: 'rgba(2, 6, 23, 0.55)', // code, logs
  solid: '#0f172a', // menus and dialogs, where see-through hurts reading
  nav: '#060b16',
  border: 'rgba(148, 163, 184, 0.12)',
  borderStrong: 'rgba(148, 163, 184, 0.22)',

  // Text (slate)
  text: '#e5e7eb',
  textMuted: '#94a3b8',
  textFaint: '#64748b',

  // Accents
  emerald: '#10b981',
  emeraldLight: '#34d399',
  mint: '#6ee7b7',
  teal: '#2dd4bf',
  ink: '#04241c', // text on the emerald gradient
  gradient: 'linear-gradient(135deg, #10b981, #2dd4bf)',
  focusRing: '0 0 0 3px rgba(16, 185, 129, 0.18)',

  // Meaning
  amber: '#fbbf24', // warnings, confirmations
  amberSurface: 'rgba(60, 24, 4, 0.6)',
  amberBorder: 'rgba(245, 158, 11, 0.6)',
  red: '#f87171',
  sky: '#38bdf8', // Dataflow
  violet: '#a78bfa', // PRL1

  // The page backdrop: two soft glows over the page colour.
  backdrop:
    'radial-gradient(1100px 560px at 12% -8%, rgba(16,185,129,.12), transparent 60%), ' +
    'radial-gradient(900px 520px at 100% 0%, rgba(45,212,191,.08), transparent 55%), #080d1a',

  font: "'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif",
  mono: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  radius: { card: 16, control: 10, pill: 999 },
} as const;
