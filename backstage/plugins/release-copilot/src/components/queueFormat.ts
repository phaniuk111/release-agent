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
