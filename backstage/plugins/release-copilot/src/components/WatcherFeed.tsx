import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Box, Button, Card, CardContent, CardHeader, Collapse, IconButton, makeStyles } from '@material-ui/core';
import RefreshIcon from '@material-ui/icons/Refresh';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import { AgentMarkdown } from './AgentMarkdown';
import { InvestigationFeedback } from './SupportTab';
import {
  FindingGroup,
  Health,
  WatchFeed,
  WatchFinding,
  asInvestigation,
  groupFindings,
  statusActions,
  statusText,
  watcherHealth,
} from './watchFormat';

const PRIORITY_COLOR: Record<string, { bg: string; fg: string }> = {
  critical: { bg: '#7f1d1d', fg: '#ffffff' },
  high: { bg: '#dc2626', fg: '#ffffff' },
  medium: { bg: '#d97706', fg: '#ffffff' },
  low: { bg: '#475569', fg: '#f1f5f9' },
};

const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  const tone = (t: Health['tone']) =>
    ({ ok: dark ? P.emeraldLight : '#047857', warn: dark ? P.amber : '#b45309', error: '#ef4444', off: theme.palette.text.secondary })[t];
  return {
    strip: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: theme.spacing(1) },
    tile: {
      padding: theme.spacing(1, 1.5),
      borderRadius: P.radius.control,
      background: dark ? P.glass : 'rgba(15,23,42,.04)',
      border: `1px solid ${theme.palette.divider}`,
    },
    tileLabel: { fontSize: '0.72rem', color: theme.palette.text.secondary },
    tileValue: { fontSize: '1.25rem', fontWeight: 600 },
    tileNote: { fontSize: '0.7rem', color: theme.palette.text.secondary },
    ok: { color: tone('ok') },
    warn: { color: tone('warn') },
    error: { color: tone('error') },
    off: { color: tone('off') },
    group: {
      marginTop: theme.spacing(2),
      fontSize: '0.75rem',
      color: theme.palette.text.secondary,
      textTransform: 'uppercase' as const,
      letterSpacing: '0.05em',
    },
    row: {
      marginTop: theme.spacing(1),
      padding: theme.spacing(1.25, 1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${theme.palette.divider}`,
      background: dark ? P.glass : 'rgba(15,23,42,.03)',
    },
    focused: { borderColor: dark ? P.sky : '#0369a1' },
    head: { display: 'flex', alignItems: 'center', gap: theme.spacing(1), flexWrap: 'wrap' as const },
    badge: {
      borderRadius: 4,
      padding: '1px 6px',
      fontSize: '0.65rem',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.05em',
    },
    title: { fontWeight: 600, fontSize: '0.875rem' },
    meta: { fontSize: '0.75rem', color: theme.palette.text.secondary },
    spacer: { flex: 1 },
    cause: { fontSize: '0.8rem', marginTop: theme.spacing(0.5) },
    established: { color: dark ? P.emeraldLight : '#047857', fontSize: '0.72rem' },
    guess: { color: dark ? P.amber : '#b45309', fontSize: '0.72rem' },
    detail: {
      marginTop: theme.spacing(1),
      padding: theme.spacing(1, 1.25),
      borderRadius: P.radius.control,
      background: dark ? 'rgba(2,6,23,.45)' : 'rgba(15,23,42,.04)',
      fontSize: '0.78rem',
    },
    label: { color: theme.palette.text.secondary },
    timeline: { margin: theme.spacing(0.5, 0, 0), paddingLeft: theme.spacing(2.5) },
    mono: { fontFamily: P.mono, fontSize: '0.72rem' },
    note: {
      fontFamily: P.mono,
      fontSize: '0.7rem',
      whiteSpace: 'pre-wrap' as const,
      marginTop: theme.spacing(0.5),
      color: theme.palette.text.secondary,
    },
    buttons: { display: 'flex', gap: theme.spacing(1), flexWrap: 'wrap' as const, marginTop: theme.spacing(1) },
    toggle: { textTransform: 'none' as const, fontSize: '0.75rem', padding: theme.spacing(0, 1) },
  };
});

function time(iso?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(11, 16);
}

function FindingRow(props: { f: WatchFinding; focused: boolean; busy: boolean; onStatus: (f: WatchFinding, s: string) => void }) {
  const classes = useStyles();
  const { f, focused, busy, onStatus } = props;
  const [open, setOpen] = useState(focused);
  const [copied, setCopied] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: 'center' });
  }, [focused]);
  const color = PRIORITY_COLOR[f.priority ?? ''] ?? PRIORITY_COLOR.low;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(f.note ?? '');
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div ref={ref} className={`${classes.row} ${focused ? classes.focused : ''}`} data-testid={`finding-${f.incident_id}`}>
      <div className={classes.head}>
        <span className={classes.badge} style={{ background: color.bg, color: color.fg }}>
          {f.priority || 'n/a'}
        </span>
        <span className={classes.title}>{f.title || f.incident_id}</span>
        <span className={classes.meta}>
          {[f.count ? `${f.count} run(s)` : '', f.owner ? `owner ${f.owner}` : '', f.category].filter(Boolean).join(' · ')}
        </span>
        <span className={classes.spacer} />
        {f.status !== 'open' && <span className={classes.meta}>{statusText(f)}</span>}
        {f.notified && <span className={classes.meta}>🔔 team notified</span>}
        <Button size="small" className={classes.toggle} onClick={() => setOpen(o => !o)}>
          {open ? 'Hide details' : 'Details'}
        </Button>
      </div>
      <div className={classes.cause}>
        <span className={classes.label}>Likely cause: </span>
        {f.cause}{' '}
        <span className={f.established ? classes.established : classes.guess}>
          {f.established ? '· established by evidence' : '· not established'}
        </span>
      </div>
      {f.do_now && (
        <div className={classes.cause}>
          <span className={classes.label}>Do now: </span>
          {f.do_now}
        </div>
      )}
      <Collapse in={open}>
        <div className={classes.detail}>
          {f.action && (
            <div>
              <span className={classes.label}>Proposed action: </span>
              <b>{f.action}</b>
              {f.action_why ? ` — ${f.action_why}` : ''}
              <span className={classes.label}> (a proposal; nothing is changed for you)</span>
            </div>
          )}
          {f.runbook && (
            <div>
              <span className={classes.label}>Runbook: </span>
              {f.runbook}
            </div>
          )}
          {(f.timeline ?? []).length > 0 && (
            <>
              <div className={classes.label} style={{ marginTop: 8 }}>
                Timeline
              </div>
              <ul className={classes.timeline}>
                {(f.timeline ?? []).map((t, i) => (
                  <li key={i}>
                    <span className={classes.mono}>{time(t.time)}</span> {t.source ? `${t.source}: ` : ''}
                    {t.text}
                  </li>
                ))}
              </ul>
            </>
          )}
          {f.answer && (
            <Box mt={1}>
              <div className={classes.label}>
                The investigation{f.model ? ` (${f.model}, ${f.model_calls ?? 0} model call(s))` : ' (no model)'}
              </div>
              <AgentMarkdown text={f.answer} />
            </Box>
          )}
          {f.note && (
            <Box mt={1}>
              <span className={classes.label}>Ticket note </span>
              <Button size="small" className={classes.toggle} onClick={copy}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
              <div className={classes.note}>{f.note}</div>
            </Box>
          )}
          <InvestigationFeedback data={asInvestigation(f)} />
        </div>
      </Collapse>
      <div className={classes.buttons}>
        {statusActions(f.status).map(a => (
          <Button key={a.status} size="small" variant="outlined" disabled={busy} onClick={() => onStatus(f, a.status)}>
            {a.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

/**
 * The support watcher: what it found in the control table on its own — each
 * new problem investigated as the Investigate button would, grouped by what a
 * person must do. Read-only: an action is a proposal; a person resolves or
 * dismisses. The bell links here (?incident=…&date=…).
 */
export function WatcherFeed() {
  const classes = useStyles();
  const apiBase = useApiBase();
  const [params] = useSearchParams();
  const focusIncident = params.get('incident') ?? '';
  const [date, setDate] = useState(params.get('date') ?? '');
  const [feed, setFeed] = useState<WatchFeed | null>(null);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (day: string) => {
      try {
        const q = day ? `?date=${encodeURIComponent(day)}` : '';
        const res = await apiGet<WatchFeed>(apiBase, `/api/support/watch${q}`);
        setFeed(res);
        setError(res.ok === false ? res.error ?? 'The findings could not be read.' : '');
        if (!day && res.business_date) setDate(res.business_date);
      } catch (e) {
        setError(String(e));
      }
    },
    [apiBase],
  );

  useEffect(() => {
    void load(date);
    // A running pass shows up here once it is done; poll while the page is open.
    const t = setInterval(() => void load(date), 30000);
    return () => clearInterval(t);
  }, [load, date]);

  const runNow = async () => {
    setRunning(true);
    setError('');
    try {
      const res = await apiPost<{ ok?: boolean; error?: string; business_date?: string }>(
        apiBase,
        '/api/support/watch/run',
        { business_date: date },
        { allowFailure: true },
      );
      if (res.ok === false) setError(res.error ?? 'Auto-triage could not run.');
      await load(res.business_date ?? date);
      if (res.business_date) setDate(res.business_date);
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };

  const changeStatus = async (f: WatchFinding, status: string) => {
    setBusy(true);
    try {
      const res = await apiPost<{ ok?: boolean; error?: string }>(
        apiBase,
        '/api/support/watch/status',
        { business_date: f.business_date, incident_id: f.incident_id, status },
        { allowFailure: true },
      );
      if (res.ok === false) setError(res.error ?? 'The change was not saved.');
      await load(date);
    } finally {
      setBusy(false);
    }
  };

  const health = watcherHealth(feed?.status);
  const groups: FindingGroup[] = groupFindings(feed?.findings);
  const open = (feed?.findings ?? []).filter(f => f.status === 'open');
  const urgent = groups.find(g => g.key === 'attention')?.items.length ?? 0;
  const last = feed?.status?.last_summary;

  return (
    <Card>
      <CardHeader
        title="Auto-triage"
        subheader={`Checks the control table on its own and investigates what is new${date ? ` — business date ${date}` : ''}`}
        action={
          <Box display="flex" alignItems="center" style={{ gap: 8 }}>
            <Button variant="outlined" size="small" disabled={running || feed?.status?.running} onClick={runNow}>
              {running || feed?.status?.running ? 'Running…' : 'Run now'}
            </Button>
            <IconButton size="small" aria-label="Refresh" onClick={() => void load(date)}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </Box>
        }
      />
      <CardContent>
        {(running || feed?.status?.running) && <Progress />}
        <div className={classes.strip}>
          <div className={classes.tile}>
            <div className={classes.tileLabel}>Watcher</div>
            <div className={`${classes.tileValue} ${classes[health.tone]}`} style={{ fontSize: '1rem' }}>
              {health.text}
            </div>
            <div className={classes.tileNote}>{health.detail}</div>
          </div>
          <div className={classes.tile}>
            <div className={classes.tileLabel}>Needs attention</div>
            <div className={classes.tileValue}>{urgent}</div>
            <div className={classes.tileNote}>open, high or critical</div>
          </div>
          <div className={classes.tile}>
            <div className={classes.tileLabel}>Open</div>
            <div className={classes.tileValue}>{open.length}</div>
            <div className={classes.tileNote}>
              {last ? `last run: ${last.investigated?.length ?? 0} investigated, ${last.waiting ?? 0} waiting` : 'not run yet'}
            </div>
          </div>
          <div className={classes.tile}>
            <div className={classes.tileLabel}>Notifications</div>
            <div className={classes.tileValue} style={{ fontSize: '1rem' }}>
              {feed?.status?.notifications ? 'On — the bell' : 'Off'}
            </div>
            <div className={classes.tileNote}>findings kept in {feed?.status?.store ?? 'memory'}</div>
          </div>
        </div>
        {error && (
          <Box mt={1} className={classes.error} style={{ fontSize: '0.8rem' }}>
            {error}
          </Box>
        )}
        {feed && groups.length === 0 && !error && (
          <Box mt={2} className={classes.meta}>
            {feed.status?.last_run_at
              ? 'Nothing open — auto-triage found no new problems.'
              : 'Auto-triage has not run for this date yet. Press Run now.'}
          </Box>
        )}
        {groups.map(g => (
          <div key={g.key}>
            <div className={classes.group}>
              {g.label} ({g.items.length})
            </div>
            {g.items.map(f => (
              <FindingRow
                key={`${f.business_date}|${f.incident_id}`}
                f={f}
                focused={f.incident_id === focusIncident}
                busy={busy}
                onStatus={changeStatus}
              />
            ))}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
