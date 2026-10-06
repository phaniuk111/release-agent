import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  IconButton,
  makeStyles,
  TextField,
  Typography,
  useTheme,
} from '@material-ui/core';
import RefreshIcon from '@material-ui/icons/Refresh';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import { TurnResult } from './TurnResult';
import {
  CAUSE_PROMPT,
  ENABLE_HINT,
  InvestigationEvent,
  MAX_CAUSE,
  SupportIncident,
  SupportReport,
  VERDICTS,
  Verdict,
  actionLabel,
  askPrompt,
  asksForCause,
  emptyText,
  feedbackPayload,
  investigatePrompt,
  jobHref,
  metaLine,
  summaryLine,
  thanksText,
} from './supportFormat';

/**
 * Support triage (L1) — the port of the portal's static/forms/support_triage.js
 * and support_feedback.js. The control table's problems for one business date
 * as a work list, most urgent first: each incident says what happened, whether
 * it is a known issue, what to do now and who owns it, with a ticket note to
 * copy. The server decides all of that (tools/support/); this renders it.
 * Every value comes from the control table, so it is rendered as React text
 * (escaped), never as HTML.
 */

const PRIORITY_COLOR: Record<string, { bg: string; fg: string }> = {
  high: { bg: '#dc2626', fg: '#ffffff' },
  medium: { bg: '#d97706', fg: '#ffffff' },
  low: { bg: '#475569', fg: '#f1f5f9' },
};
// Tailwind *-300 on the dark page, *-700 on a light one (the portal is dark only).
const ACTION_COLOR: Record<string, { dark: string; light: string }> = {
  wait: { dark: '#7dd3fc', light: '#0369a1' },
  retrigger: { dark: '#6ee7b7', light: '#047857' },
  check: { dark: '#fcd34d', light: '#b45309' },
  escalate: { dark: '#fca5a5', light: '#b91c1c' },
};
const VERDICT_COLOR: Record<Verdict, { dark: string; light: string }> = {
  right: ACTION_COLOR.retrigger,
  direction: ACTION_COLOR.check,
  wrong: ACTION_COLOR.escalate,
};

const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  return {
    head: {
      display: 'flex',
      alignItems: 'center',
      flexWrap: 'wrap' as const,
      gap: theme.spacing(1),
    },
    spacer: { flex: 1 },
    summary: { fontSize: '0.8rem', color: theme.palette.text.secondary },
    meta: { fontSize: '0.72rem', color: theme.palette.text.secondary, opacity: 0.8 },
    warn: { fontSize: '0.8rem', color: dark ? P.amber : '#b45309' },
    clear: { fontSize: '0.8rem', color: dark ? P.emeraldLight : '#047857' },
    card: {
      marginTop: theme.spacing(1),
      padding: theme.spacing(1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${theme.palette.divider}`,
      background: dark ? P.glass : 'rgba(15,23,42,.03)',
    },
    badge: {
      borderRadius: 4,
      padding: '1px 6px',
      fontSize: '0.65rem',
      textTransform: 'uppercase' as const,
      letterSpacing: '0.05em',
    },
    title: { fontWeight: 600, fontSize: '0.875rem' },
    reason: { fontSize: '0.68rem', color: theme.palette.text.secondary },
    action: { fontSize: '0.75rem', fontWeight: 600, whiteSpace: 'nowrap' as const },
    small: { fontSize: '0.75rem', marginTop: theme.spacing(0.5) },
    error: {
      fontFamily: P.mono,
      fontSize: '0.68rem',
      color: theme.palette.text.secondary,
      marginTop: theme.spacing(0.5),
      maxWidth: '48rem',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap' as const,
    },
    jobs: { fontSize: '0.68rem', color: theme.palette.text.secondary, marginTop: theme.spacing(0.5) },
    mono: { fontFamily: P.mono },
    link: { fontFamily: P.mono, color: dark ? P.sky : '#0369a1' },
    label: { color: theme.palette.text.secondary },
    known: { color: dark ? P.mint : '#047857' },
    steps: { fontSize: '0.75rem', margin: theme.spacing(0.5, 0, 0), paddingLeft: theme.spacing(2.5) },
    footer: {
      display: 'flex',
      alignItems: 'center',
      flexWrap: 'wrap' as const,
      gap: theme.spacing(1),
      marginTop: theme.spacing(1),
      fontSize: '0.75rem',
    },
    note: { fontSize: '0.68rem', color: theme.palette.text.secondary, marginTop: theme.spacing(0.5) },
    feedback: {
      display: 'flex',
      alignItems: 'center',
      flexWrap: 'wrap' as const,
      gap: theme.spacing(1),
      marginTop: theme.spacing(1.5),
      fontSize: '0.75rem',
    },
    cause: { display: 'flex', alignItems: 'center', gap: theme.spacing(1), width: '100%' },
  };
});

function useDark(): boolean {
  return useTheme().palette.type === 'dark';
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // No clipboard permission (plain http, an old browser): select it instead.
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
    } catch {
      /* nothing more to try */
    }
    ta.remove();
  }
}

function CopyNote(props: { note: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Button
      size="small"
      color="primary"
      onClick={async () => {
        await copyText(props.note);
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? 'Copied' : 'Copy ticket note'}
    </Button>
  );
}

function IncidentCard(props: {
  inc: SupportIncident;
  report: SupportReport;
  llm: boolean;
  busy: boolean;
  onSend: (text: string) => Promise<void> | void;
}) {
  const classes = useStyles();
  const { inc, report, llm, busy, onSend } = props;
  const dark = useDark();
  const pri = PRIORITY_COLOR[inc.priority ?? ''] || PRIORITY_COLOR.low;
  const act = ACTION_COLOR[inc.action ?? ''] || ACTION_COLOR.check;
  const jobs = (Array.isArray(inc.job_ids) ? inc.job_ids : []).filter(Boolean).map(String);
  return (
    <Box className={classes.card} data-testid="support-incident">
      <Box className={classes.head}>
        <span className={classes.badge} style={{ background: pri.bg, color: pri.fg }}>
          {inc.priority || 'low'}
        </span>
        <span className={classes.title}>{inc.title || ''}</span>
        {inc.priority_reason && <span className={classes.reason}>{inc.priority_reason}</span>}
        <span className={classes.spacer} />
        <span className={classes.action} style={{ color: dark ? act.dark : act.light }}>
          → {actionLabel(inc.action)}
        </span>
      </Box>
      <Box className={classes.small}>
        {(inc.facts || []).map((f, i) => (
          <div key={i}>{f}</div>
        ))}
      </Box>
      {inc.error_text && (
        <div className={classes.error} title={inc.error_text}>
          {inc.error_text}
        </div>
      )}
      {jobs.length > 0 && (
        <div className={classes.jobs}>
          jobs:{' '}
          {jobs.map((id, i) => {
            const href = jobHref(report.job_url, id);
            return (
              <span key={id}>
                {i > 0 && ', '}
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" className={classes.link}>
                    {id}
                  </a>
                ) : (
                  <span className={classes.mono}>{id}</span>
                )}
              </span>
            );
          })}
        </div>
      )}
      <Box className={classes.small} style={{ marginTop: 8 }}>
        <span className={classes.label}>Known issue:</span>{' '}
        {inc.runbook ? (
          <span className={classes.known}>{inc.runbook}</span>
        ) : (
          <span className={classes.label}>no runbook match</span>
        )}
      </Box>
      {(inc.steps || []).length > 0 && (
        <ol className={classes.steps}>
          {(inc.steps || []).map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      )}
      <Box className={classes.footer}>
        <span>
          <span className={classes.label}>Owner:</span> {inc.owner || ''}
        </span>
        <span className={classes.spacer} />
        <CopyNote note={String(inc.note || '')} />
        {/* Investigate and Ask why are the model's job: no model, no buttons. */}
        {llm && (
          <>
            <Button
              size="small"
              variant="outlined"
              color="primary"
              disabled={busy}
              onClick={() => onSend(investigatePrompt(inc, report))}
            >
              Investigate
            </Button>
            <Button size="small" color="primary" disabled={busy} onClick={() => onSend(askPrompt(inc, report))}>
              Ask why
            </Button>
          </>
        )}
      </Box>
    </Box>
  );
}

/** "Was this right?" under a complete investigation. Who answered is decided by
 *  the server (the verified caller); nothing but the answer is sent from here. */
function InvestigationFeedback(props: { data: InvestigationEvent }) {
  const classes = useStyles();
  const dark = useDark();
  const apiBase = useApiBase();
  const [picked, setPicked] = useState<Verdict | ''>('');
  const [cause, setCause] = useState('');
  const [sending, setSending] = useState(false);
  const [saved, setSaved] = useState<Verdict | ''>('');
  const [error, setError] = useState('');
  const [disabled, setDisabled] = useState(false);

  const send = useCallback(
    async (verdict: Verdict) => {
      if (sending) return;
      setSending(true);
      setError('');
      let res: { ok?: boolean; error?: string; hint?: string; disabled?: boolean };
      try {
        res = await apiPost(apiBase, '/api/support/feedback', feedbackPayload(props.data, verdict, cause), {
          allowFailure: true,
        });
      } catch (e) {
        res = { ok: false, error: String((e as Error)?.message || e) };
      }
      setSending(false);
      if (res && res.ok) {
        setSaved(verdict);
        return;
      }
      setError(((res && res.error) || 'no answer') + (res && res.hint ? ` ${res.hint}` : ''));
      if (res && res.disabled) setDisabled(true);
    },
    [apiBase, props.data, cause, sending],
  );

  if (saved) {
    return (
      <Box className={classes.feedback}>
        <span className={classes.label}>✓ {thanksText(saved)}</span>
      </Box>
    );
  }
  return (
    <Box className={classes.feedback} data-testid="support-feedback">
      <span className={classes.label}>Was this right?</span>
      {!disabled &&
        VERDICTS.map(v => (
          <Button
            key={v.key}
            size="small"
            variant="outlined"
            disabled={sending}
            style={{ color: dark ? VERDICT_COLOR[v.key].dark : VERDICT_COLOR[v.key].light }}
            onClick={() => {
              setPicked(v.key);
              if (!asksForCause(v.key)) send(v.key);
            }}
          >
            {v.label}
          </Button>
        ))}
      {!disabled && asksForCause(picked) && (
        <Box className={classes.cause}>
          <TextField
            fullWidth
            size="small"
            variant="outlined"
            autoFocus
            placeholder={CAUSE_PROMPT}
            value={cause}
            onChange={e => setCause(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && picked) {
                e.preventDefault();
                send(picked);
              }
            }}
            inputProps={{ maxLength: MAX_CAUSE, 'aria-label': CAUSE_PROMPT }}
          />
          <Button
            size="small"
            variant="outlined"
            color="primary"
            disabled={sending}
            onClick={() => picked && send(picked)}
          >
            Save
          </Button>
        </Box>
      )}
      {error && (
        <Typography className={classes.warn} style={{ width: '100%' }}>
          {error}
        </Typography>
      )}
    </Box>
  );
}

export function SupportTab(props: {
  onSend: (text: string) => Promise<void> | void;
  /** Any turn is in flight (one at a time, across tabs). */
  busy?: boolean;
  /** The reply to THIS tab's latest message (Investigate / Ask why). */
  result?: { text: string; streaming: boolean; pendingToken: string | null };
  /** LLM_ENABLED: Investigate and Ask why need a model. */
  llm?: boolean;
  /** The stream's "investigation" event: an Investigate answer is complete. */
  investigation?: {
    business_date: string;
    incident_id: string;
    title?: string;
    model?: string;
    model_calls?: number;
    seconds?: number;
    shared?: boolean;
  } | null;
}) {
  const classes = useStyles();
  const {
    onSend,
    busy = false,
    result = { text: '', streaming: false, pendingToken: null },
    llm = true,
    investigation = null,
  } = props;
  const apiBase = useApiBase();
  // Empty = the latest business date in the table.
  const [date, setDate] = useState('');
  const [report, setReport] = useState<SupportReport | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(
    async (day: string, fresh: boolean) => {
      // A slower answer for a date you already left must not land.
      const mine = ++seq.current;
      setLoading(true);
      let res: SupportReport;
      try {
        const q = new URLSearchParams({ date: day, fresh: fresh ? '1' : '0' });
        res = await apiGet<SupportReport>(apiBase, `/api/support/triage?${q.toString()}`);
      } catch (e) {
        res = { ok: false, error: String((e as Error)?.message || e) };
      }
      if (!res || typeof res !== 'object') res = { ok: false, error: 'no answer' };
      if (mine !== seq.current) return;
      setReport(res);
      setLoading(false);
    },
    [apiBase],
  );

  useEffect(() => {
    load(date, false);
  }, [date, load]);

  const r = report ?? {};
  const incidents = (Array.isArray(r.incidents) ? r.incidents : []).map(inc =>
    inc && typeof inc === 'object' ? inc : {},
  );
  const notes = Array.isArray(r.notes) ? r.notes : [];

  let body;
  if (loading) {
    body = (
      <Box>
        <Progress />
        <Typography className={classes.meta}>Reading the control table…</Typography>
      </Box>
    );
  } else if (r.disabled) {
    body = <Typography className={classes.warn}>{ENABLE_HINT}</Typography>;
  } else if (r.ok === false) {
    body = (
      <>
        <Typography className={classes.warn}>{r.error || 'no answer'}</Typography>
        {r.hint && (
          <Typography className={classes.warn} style={{ opacity: 0.8, marginTop: 4 }}>
            {r.hint}
          </Typography>
        )}
      </>
    );
  } else if (!incidents.length) {
    body = <Typography className={classes.clear}>✓ {emptyText(r)}</Typography>;
  } else {
    body = incidents.map((inc, i) => (
      <IncidentCard key={`${inc.id ?? ''}-${i}`} inc={inc} report={r} llm={llm} busy={busy} onSend={onSend} />
    ));
  }

  return (
    <Card>
      <CardHeader title="Support triage" subheader="The control table's problems for one business date, most urgent first" />
      <CardContent>
        <Box className={classes.head}>
          <Typography className={classes.summary}>{report && !loading ? summaryLine(r) : ''}</Typography>
          <span className={classes.spacer} />
          <TextField
            type="date"
            size="small"
            variant="outlined"
            label="Business date"
            InputLabelProps={{ shrink: true }}
            value={date || (report && !loading ? r.business_date || '' : '')}
            onChange={e => setDate(e.target.value)}
            inputProps={{ 'aria-label': 'Business date' }}
          />
          <IconButton size="small" title="Read again now" aria-label="Read again now" onClick={() => load(date, true)}>
            <RefreshIcon fontSize="small" />
          </IconButton>
        </Box>
        {report && !loading && <Typography className={classes.meta}>{metaLine(r)}</Typography>}
        <Box style={{ marginTop: 8 }}>{body}</Box>
        {!loading &&
          notes.map((n, i) => (
            <Typography key={i} className={classes.note}>
              ⓘ {n}
            </Typography>
          ))}
        <TurnResult {...result} onConfirm={() => {}} onCancel={() => {}} />
        {investigation && (
          <InvestigationFeedback
            key={`${investigation.business_date}|${investigation.incident_id}|${investigation.model_calls ?? ''}|${
              investigation.seconds ?? ''
            }`}
            data={investigation}
          />
        )}
      </CardContent>
    </Card>
  );
}
