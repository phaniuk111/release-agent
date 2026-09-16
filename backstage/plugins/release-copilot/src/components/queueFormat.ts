/**
 * Wording for queue results — the same sentences the portal shows. Pure, so it
 * is tested without rendering anything.
 */

export type Refusal = {
  artifact?: string;
  error?: string;
  reason?: string;
  failed_controls?: string[];
  failed_controls_detail?: Array<{ control?: string; job?: string }>;
  failed_steps?: Array<{ name?: string; job?: string } | string>;
  open_controls?: Array<{ control?: string; job?: string; status?: string; conclusion?: string }>;
  run_url?: string;
};

/**
 * Why a row was not queued. A failed control arrives as `reason` plus the
 * controls (not `error`), so reading only `error` printed "undefined" — and
 * "not eligible" without the control's name and job sends nobody anywhere.
 */
export function describeRefusal(r: Refusal): string {
  const parts: string[] = [];
  const detail = r.failed_controls_detail ?? [];
  if (detail.length) {
    detail.forEach(c => parts.push(`control ${c.control}${c.job ? ` in job ${c.job}` : ''} failed`));
  } else {
    (r.failed_controls ?? []).forEach(c => parts.push(`control ${c} failed`));
  }
  (r.failed_steps ?? []).forEach(st =>
    parts.push(
      typeof st === 'string'
        ? `step ${st} failed`
        : `step ${st.name}${st.job ? ` in job ${st.job}` : ''} failed`,
    ),
  );
  (r.open_controls ?? []).forEach(c =>
    parts.push(
      `control ${c.control}${c.job ? ` in job ${c.job}` : ''} not passed yet (${
        c.status || c.conclusion || 'not run'
      })`,
    ),
  );
  if (parts.length) return parts.join('; ');
  return r.error || r.reason || 'not eligible';
}

/** "5m ago" / "3h ago" / "2d ago"; '' when there is no time. */
export function timeAgo(iso?: string, now: number = Date.now()): string {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, (now - t) / 1000);
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** "dev@example.com" → "dev". */
export const shortName = (email?: string) => String(email ?? '').split('@')[0];

/** "RCTLDEF0001691 - Peer review evidence in job x" → "1691" (the ID's number). */
function controlNumber(entry: string): string {
  const id = entry.trim().split(/[\s-]/)[0] ?? '';
  const digits = id.match(/\d+$/)?.[0];
  return digits ? String(parseInt(digits, 10)) : id;
}

/**
 * The release queue's Controls column — the one place an allowed control shows.
 * It failed on the build run but may be a false positive, so it reads "open"
 * (to close by hand), never "failed"; the release is not stopped by it.
 * Mirrors the portal's core/queue.js controlsSummary.
 */
export function controlsSummary(q: { build_verified?: boolean | null; allowed_failures?: string }): {
  state: 'passed' | 'open' | 'unknown';
  label: string;
  title: string;
} {
  const allowed = String(q?.allowed_failures ?? '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
  if (allowed.length) {
    return {
      state: 'open',
      label: `${allowed.map(controlNumber).join(', ')} open`,
      title: `${allowed.join('\n')}\n— failed on the build run, possibly a false positive: close it manually. Every other control passed.`,
    };
  }
  if (q?.build_verified === true) {
    return { state: 'passed', label: 'all passed', title: 'every release control passed on the build run' };
  }
  return { state: 'unknown', label: 'not checked', title: 'no verified build run at queue time' };
}

/** What the Add dialog says after queueing — naming a control left open. */
export function queuedNotice(queued: Array<{ artifact?: string; allowed_failures?: string[] }>): string {
  const names = queued.map(q => q.artifact).filter(Boolean).join(', ');
  const open = queued.flatMap(q => q.allowed_failures ?? []);
  if (open.length) {
    return `Queued ${names} — ${open.map(controlNumber).join(', ')} open: it failed on the build run and may be a false positive, so close it manually. Every other control passed.`;
  }
  return `Queued ${names} — build and controls passed.`;
}
