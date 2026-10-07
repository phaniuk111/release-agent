/**
 * The support watcher's feed: pure rules the screen renders (grouping, the
 * watcher's health line). The server decides every finding
 * (src/release_agent/tools/support/watch.py); nothing here judges a cause.
 */
import type { InvestigationEvent } from './supportFormat';

export type WatchFinding = {
  business_date: string;
  incident_id: string;
  status: 'open' | 'dismissed' | 'resolved' | 'cleared' | string;
  title?: string;
  category?: string;
  priority?: string;
  count?: number;
  owner?: string;
  runbook?: string;
  cause?: string;
  established?: boolean;
  do_now?: string;
  action?: string;
  action_why?: string;
  timeline?: { time?: string; source?: string; text?: string }[];
  note?: string;
  answer?: string;
  model?: string;
  model_calls?: number;
  seconds?: number;
  fallback?: boolean;
  notified?: boolean;
  investigated_at?: string;
  changed_at?: string;
  actor?: string;
};

export type WatchStatus = {
  enabled?: boolean;
  interval_minutes?: number;
  running?: boolean;
  last_run_at?: string | null;
  last_business_date?: string;
  last_error?: string;
  last_summary?: { investigated?: string[]; waiting?: number; cleared?: string[]; notified?: number } | null;
  store?: string;
  notifications?: boolean;
};

export type WatchFeed = {
  ok?: boolean;
  error?: string;
  business_date?: string;
  findings?: WatchFinding[];
  status?: WatchStatus;
};

export type FindingGroup = { key: 'attention' | 'open' | 'closed'; label: string; items: WatchFinding[] };

const URGENT = new Set(['critical', 'high']);

/** Needs attention (open, high or critical), then the other open ones, then the closed. */
export function groupFindings(findings: WatchFinding[] | undefined): FindingGroup[] {
  const all = findings ?? [];
  const open = all.filter(f => f.status === 'open');
  return [
    { key: 'attention' as const, label: 'Needs attention', items: open.filter(f => URGENT.has(f.priority ?? '')) },
    { key: 'open' as const, label: 'Open', items: open.filter(f => !URGENT.has(f.priority ?? '')) },
    { key: 'closed' as const, label: 'Resolved, dismissed and cleared', items: all.filter(f => f.status !== 'open') },
  ].filter(g => g.items.length > 0);
}

function minutesSince(iso: string | null | undefined, now: number): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : Math.max(0, Math.round((now - t) / 60000));
}

export function ago(minutes: number): string {
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const h = Math.round(minutes / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}

export type Health = { tone: 'ok' | 'warn' | 'error' | 'off'; text: string; detail: string };

/**
 * One line on whether the watcher is doing its job. A watcher that stopped
 * must be obvious: no run for twice its interval is a warning, not silence.
 */
export function watcherHealth(status: WatchStatus | undefined, now: number = Date.now()): Health {
  const s = status ?? {};
  const since = minutesSince(s.last_run_at, now);
  const every = s.interval_minutes ?? 0;
  if (s.running) return { tone: 'ok', text: 'Running now', detail: 'Investigating new problems' };
  if (s.last_error) {
    return { tone: 'error', text: 'Last run failed', detail: s.last_error };
  }
  if (!s.enabled) {
    return {
      tone: 'off',
      text: since === null ? 'Not scheduled' : `Ran ${ago(since)}`,
      detail: 'Runs only when you press Run now (SUPPORT_WATCH_MINUTES is 0)',
    };
  }
  if (since === null) return { tone: 'warn', text: 'Has not run yet', detail: `Runs every ${every} min` };
  if (since > 2 * every) return { tone: 'warn', text: `Ran ${ago(since)}`, detail: `Overdue — it runs every ${every} min` };
  return { tone: 'ok', text: `Ran ${ago(since)}`, detail: `Next in about ${Math.max(0, every - since)} min` };
}

/** What a finding offers a person, by its status. */
export function statusActions(status: string): { status: string; label: string }[] {
  if (status === 'open') {
    return [
      { status: 'resolved', label: 'Mark resolved' },
      { status: 'dismissed', label: 'Dismiss' },
    ];
  }
  return [{ status: 'open', label: 'Reopen' }];
}

export function statusText(f: WatchFinding): string {
  const who = f.actor && f.actor !== 'watcher' ? ` by ${f.actor}` : '';
  switch (f.status) {
    case 'resolved':
      return `Resolved${who}`;
    case 'dismissed':
      return `Dismissed${who}`;
    case 'cleared':
      return 'Cleared — no longer in the control table';
    default:
      return 'Open';
  }
}

/** The finding as "Was this right?" records it. */
export function asInvestigation(f: WatchFinding): InvestigationEvent {
  return {
    business_date: f.business_date,
    incident_id: f.incident_id,
    title: f.title,
    category: f.category,
    action: f.action,
    model: f.model,
    model_calls: f.model_calls ?? null,
    seconds: f.seconds ?? null,
  };
}
