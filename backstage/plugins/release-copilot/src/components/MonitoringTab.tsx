import {
  Fragment,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Button,
  Grid,
  IconButton,
  makeStyles,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@material-ui/core';
import RefreshIcon from '@material-ui/icons/Refresh';
import { InfoCard, Progress } from '@backstage/core-components';
import { useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import { TurnResult } from './TurnResult';
import {
  ALERT_POLICY_COMMAND,
  ALERT_POLICY_LEAD,
  CHECK_STATE,
  ENABLE_HINT,
  NOTHING_MEASURED,
  PARTIAL_LEAD,
  accessLines,
  checkExplainPrompt,
  costSummary,
  emptyText,
  extrasText,
  formatBytes,
  formatCount,
  formatGb,
  formatUsd,
  formatValue,
  insightText,
  monitoringSummary,
  orderChecks,
  orderShapes,
  reportMeta,
  seriesLabel,
  shapeExplainPrompt,
  unmeasuredText,
  watchingText,
  whoText,
  type BqReport,
  type BqShape,
  type CheckState,
  type MonitorCheck,
  type MonitorResult,
} from './monitoringFormat';

// The Monitoring tab — the portal's two Monitoring pills (static/forms/bq_cost.js
// and static/forms/monitoring.js) as two cards, each loading on its own so a slow
// INFORMATION_SCHEMA scan never holds the PromQL checks back. The server
// measures and decides; these cards only show it. "Ask why" is the model's job:
// no model (llm=false), no button — and the reply lands under the card that asked.

type TurnReply = {
  text: string;
  streaming: boolean;
  pendingToken: string | null;
};

/**
 * The portal's api.js result shape: a network failure throws (as fetch does);
 * an HTTP error comes back as {ok: false, status, error, ...body}, so a refused
 * request (403 from the preview gate) and a crashed one read the same way.
 */
async function getJson<T extends { ok?: boolean; error?: string }>(
  base: string,
  path: string,
): Promise<T> {
  const r = await fetch(`${base}${path}`);
  let data: unknown = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  const body =
    data && typeof data === 'object' && !Array.isArray(data)
      ? (data as Record<string, unknown>)
      : null;
  if (!r.ok) {
    const reason = body && (body.error || body.detail);
    return {
      ...(body || {}),
      ok: false,
      status: r.status,
      error: reason
        ? String(typeof reason === 'string' ? reason : JSON.stringify(reason))
        : `HTTP ${r.status}`,
    } as unknown as T;
  }
  if (body === null) {
    return {
      ok: false,
      error: 'the answer was not JSON — a proxy or login page?',
    } as T;
  }
  return body as T;
}

const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  const tone = {
    red: dark ? P.red : '#b91c1c',
    amber: dark ? P.amber : '#b45309',
    emerald: dark ? P.emeraldLight : '#047857',
    faint: theme.palette.text.secondary,
  };
  return {
    summary: { fontSize: '0.8rem', color: theme.palette.text.secondary },
    meta: {
      fontSize: '0.7rem',
      color: theme.palette.text.secondary,
      marginBottom: theme.spacing(1),
    },
    headerActions: {
      display: 'flex',
      alignItems: 'center',
      gap: theme.spacing(1),
    },
    link: {
      fontSize: '0.75rem',
      color: tone.emerald,
      whiteSpace: 'nowrap' as const,
    },
    warn: {
      fontSize: '0.8rem',
      color: tone.amber,
      marginBottom: theme.spacing(1),
    },
    muted: { fontSize: '0.8rem', color: theme.palette.text.secondary },
    access: {
      marginBottom: theme.spacing(1),
      padding: theme.spacing(1, 1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${P.amberBorder}`,
      background: dark ? P.amberSurface : 'rgba(245, 158, 11, 0.08)',
      fontSize: '0.75rem',
      color: tone.amber,
    },
    accessLine: { paddingLeft: theme.spacing(2) },
    accessNote: { paddingLeft: theme.spacing(3), opacity: 0.8 },
    role: { fontFamily: P.mono },
    table: {
      '& td, & th': {
        fontSize: '0.75rem',
        padding: theme.spacing(0.5, 1),
        verticalAlign: 'top',
      },
    },
    num: { textAlign: 'right' as const, whiteSpace: 'nowrap' as const },
    nowrap: { whiteSpace: 'nowrap' as const },
    insight: { color: tone.amber },
    sql: {
      fontFamily: P.mono,
      fontSize: '0.65rem',
      color: theme.palette.text.secondary,
      display: 'block',
      maxWidth: '26rem',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap' as const,
    },
    ask: {
      textTransform: 'none' as const,
      fontSize: '0.75rem',
      whiteSpace: 'nowrap' as const,
      minWidth: 0,
    },
    check: {
      borderTop: `1px solid ${theme.palette.divider}`,
      padding: theme.spacing(1, 0),
    },
    checkHead: {
      display: 'flex',
      alignItems: 'center',
      gap: theme.spacing(1),
      flexWrap: 'wrap' as const,
    },
    checkName: { fontWeight: 500, fontSize: '0.875rem' },
    severity: { fontSize: '0.65rem', color: tone.amber },
    spacer: { flex: 1 },
    query: {
      fontFamily: P.mono,
      fontSize: '0.65rem',
      color: theme.palette.text.secondary,
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap' as const,
    },
    series: {
      fontSize: '0.75rem',
      marginTop: theme.spacing(0.5),
      borderCollapse: 'collapse' as const,
    },
    seriesLabel: {
      fontFamily: P.mono,
      paddingRight: theme.spacing(1.5),
      whiteSpace: 'nowrap' as const,
    },
    seriesValue: { color: tone.red, whiteSpace: 'nowrap' as const },
    fold: {
      borderTop: `1px solid ${theme.palette.divider}`,
      paddingTop: theme.spacing(1),
      fontSize: '0.75rem',
    },
    foldSummary: { cursor: 'pointer', color: theme.palette.text.secondary },
    policy: {
      margin: theme.spacing(0.5, 0),
      padding: theme.spacing(0.5, 1),
      borderRadius: P.radius.control,
      border: `1px solid ${theme.palette.divider}`,
      background: dark ? P.sunken : 'rgba(15,23,42,.04)',
      fontFamily: P.mono,
      fontSize: '0.7rem',
      overflowX: 'auto' as const,
    },
    command: {
      fontFamily: P.mono,
      fontSize: '0.65rem',
      color: theme.palette.text.secondary,
    },
    toneRed: { color: tone.red, whiteSpace: 'nowrap' as const },
    toneAmber: { color: tone.amber, whiteSpace: 'nowrap' as const },
    toneEmerald: { color: tone.emerald, whiteSpace: 'nowrap' as const },
    toneFaint: { color: tone.faint, whiteSpace: 'nowrap' as const },
  };
});

// ---- BigQuery cost --------------------------------------------------------------
// One table: the most expensive query shapes, straight from /api/bq-cost/report.
// The full report (storage, writes, history) is the Excel download, built
// server-side from the same cached scan.

function BqCostCard(props: {
  apiBase: string;
  llm: boolean;
  busy: boolean;
  onAsk: (text: string) => void;
  children?: ReactNode;
}) {
  const classes = useStyles();
  const { apiBase, llm, busy, onAsk } = props;
  const [res, setRes] = useState<BqReport | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(
    async (fresh: boolean) => {
      const mine = ++seq.current;
      setLoading(true);
      let out: BqReport;
      try {
        out = await getJson<BqReport>(
          apiBase,
          `/api/bq-cost/report?fresh=${fresh ? 1 : 0}`,
        );
      } catch (e) {
        out = { ok: false, error: String((e as Error)?.message ?? e) };
      }
      if (!out || typeof out !== 'object')
        out = { ok: false, error: 'no answer' };
      if (mine !== seq.current) return;
      setRes(out);
      setLoading(false);
    },
    [apiBase],
  );

  useEffect(() => {
    load(false);
  }, [load]);

  const shapes = res ? orderShapes(res.shapes, res.billing) : [];
  const extras = res ? extrasText(res) : '';
  const access = res ? accessLines(res) : [];
  const meta = res ? reportMeta(res) : '';

  const action = (
    <div className={classes.headerActions}>
      {res && res.ok !== false && (
        <a
          href={`${apiBase}/api/bq-cost/report.xlsx`}
          download
          className={classes.link}
          title="The whole report as a workbook — Summary, Top queries, Storage, Writes, History"
        >
          Download Excel
        </a>
      )}
      <IconButton
        size="small"
        aria-label="Scan again now"
        title="Scan again now"
        disabled={loading}
        onClick={() => load(true)}
      >
        <RefreshIcon fontSize="small" />
      </IconButton>
    </div>
  );

  return (
    <InfoCard
      title="BigQuery cost report"
      subheader={
        res ? (
          <span className={classes.summary}>{costSummary(res)}</span>
        ) : undefined
      }
      action={action}
    >
      {loading && !res && (
        <>
          <Typography className={classes.muted}>
            Reading INFORMATION_SCHEMA…
          </Typography>
          <Progress />
        </>
      )}
      {loading && res && <Progress />}
      {res && (
        <>
          {(meta || extras) && (
            <Typography className={classes.meta}>
              {meta}
              {extras ? ` · ${extras}` : ''}
            </Typography>
          )}
          {res.hint && (
            <Typography className={classes.warn}>{res.hint}</Typography>
          )}
          {/* A partial report: what this account could read is below; this says
              which role unlocks the rest, and where to grant it. */}
          {access.length > 0 && (
            <div className={classes.access} data-testid="bq-access">
              <div>{PARTIAL_LEAD}</div>
              {access.map(a => (
                <div key={a.role} className={classes.accessLine}>
                  • <span className={classes.role}>{a.role}</span> on{' '}
                  {a.grantOn} — unlocks {a.unlocks}{' '}
                  <span style={{ opacity: 0.8 }}>({a.permission})</span>
                  {a.note && <div className={classes.accessNote}>{a.note}</div>}
                </div>
              ))}
            </div>
          )}
          {res.disabled && (
            <Typography className={classes.warn}>{ENABLE_HINT}</Typography>
          )}
          {/* The heading already says it is unavailable — this is the reason, once. */}
          {!res.disabled && res.ok === false && (
            <Typography className={classes.warn}>
              {res.error || 'no answer'}
            </Typography>
          )}
          {!res.disabled && res.ok !== false && !shapes.length && (
            <Typography className={classes.muted}>{emptyText(res)}</Typography>
          )}
          {!res.disabled && res.ok !== false && shapes.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <Table
                size="small"
                className={classes.table}
                aria-label="Most expensive query shapes"
              >
                <TableHead>
                  <TableRow>
                    <TableCell className={classes.num}>#</TableCell>
                    <TableCell>who</TableCell>
                    <TableCell className={classes.num}>runs</TableCell>
                    <TableCell className={classes.num}>billed</TableCell>
                    <TableCell className={classes.num}>≈ $</TableCell>
                    <TableCell className={classes.num}>p50 / run</TableCell>
                    <TableCell>insight</TableCell>
                    <TableCell>query</TableCell>
                    <TableCell className={classes.num} />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {shapes.map((raw, i) => (
                    <ShapeRow
                      key={`${String((raw && raw.qhash) || '')}-${i}`}
                      shape={raw && typeof raw === 'object' ? raw : {}}
                      index={i}
                      llm={llm}
                      busy={busy}
                      onAsk={onAsk}
                    />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </>
      )}
      {props.children}
    </InfoCard>
  );
}

function ShapeRow(props: {
  shape: BqShape;
  index: number;
  llm: boolean;
  busy: boolean;
  onAsk: (text: string) => void;
}) {
  const classes = useStyles();
  const { shape: s, index, llm, busy, onAsk } = props;
  const users = (Array.isArray(s.users) ? s.users : [])
    .filter(Boolean)
    .map(String);
  const preview = String(s.sql_preview || '');
  const insight = insightText(s.insights);
  return (
    <TableRow data-testid="bq-shape">
      <TableCell className={classes.num} style={{ opacity: 0.6 }}>
        {index + 1}
      </TableCell>
      <TableCell className={classes.nowrap}>
        <span title={users.join(', ') || String(s.who || '')}>
          {whoText(s)}
        </span>
      </TableCell>
      <TableCell className={classes.num}>{formatCount(s.runs)}</TableCell>
      <TableCell className={classes.num}>{formatGb(s.gb_billed)}</TableCell>
      <TableCell className={classes.num}>{formatUsd(s.approx_usd)}</TableCell>
      <TableCell className={classes.num}>
        {s.p50_bytes !== null && s.p50_bytes !== undefined
          ? formatBytes(s.p50_bytes)
          : formatGb(s.p50_gb)}
      </TableCell>
      <TableCell>
        {insight && <span className={classes.insight}>{insight}</span>}
      </TableCell>
      <TableCell>
        {/* The preview is 180 characters of someone's SQL: one truncated line, the whole of it on hover. */}
        <span className={classes.sql} title={preview}>
          {preview}
        </span>
      </TableCell>
      <TableCell className={classes.num}>
        {llm && (
          <Button
            size="small"
            color="primary"
            className={classes.ask}
            disabled={busy}
            onClick={() => onAsk(shapeExplainPrompt(s))}
          >
            Ask why
          </Button>
        )}
      </TableCell>
    </TableRow>
  );
}

// ---- PromQL checks -----------------------------------------------------------------
// The checks, run now, straight from /api/monitoring — the server decides the
// state (tools/monitoring.py). Four states, none of them silent: firing, could
// not run, OK (with what it watched), and "not measured here" — a check with
// nothing to measure is not a healthy one.

function ChecksCard(props: {
  apiBase: string;
  llm: boolean;
  busy: boolean;
  onAsk: (text: string) => void;
  children?: ReactNode;
}) {
  const classes = useStyles();
  const { apiBase, llm, busy, onAsk } = props;
  const [res, setRes] = useState<MonitorResult | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(
    async (fresh: boolean) => {
      const mine = ++seq.current;
      setLoading(true);
      let out: MonitorResult;
      try {
        out = await getJson<MonitorResult>(
          apiBase,
          `/api/monitoring${fresh ? '?fresh=1' : ''}`,
        );
      } catch (e) {
        out = { ok: false, error: String((e as Error)?.message ?? e) };
      }
      if (mine !== seq.current) return;
      setRes(out);
      setLoading(false);
    },
    [apiBase],
  );

  useEffect(() => {
    load(false);
  }, [load]);

  const checks = orderChecks((res && res.checks) || []);
  const measured = checks.filter(c => c.state !== 'no_data');
  const unmeasured = checks.filter(c => c.state === 'no_data');
  const meta = res
    ? `${res.source || ''}${res.checked_at ? ` · ${res.checked_at}` : ''}`
    : '';

  const action = (
    <IconButton
      size="small"
      aria-label="Run the checks again"
      title="Run the checks again"
      disabled={loading}
      onClick={() => load(true)}
    >
      <RefreshIcon fontSize="small" />
    </IconButton>
  );

  const row = (c: MonitorCheck, i: number) => (
    <CheckRow
      key={`${c.name}-${i}`}
      check={c}
      apiBase={apiBase}
      llm={llm}
      busy={busy}
      onAsk={onAsk}
    />
  );

  return (
    <InfoCard
      title="Monitoring"
      subheader={
        res ? (
          <span className={classes.summary}>{monitoringSummary(res)}</span>
        ) : undefined
      }
      action={action}
    >
      {loading && !res && (
        <>
          <Typography className={classes.muted}>Running the checks…</Typography>
          <Progress />
        </>
      )}
      {loading && res && <Progress />}
      {res && (
        <>
          {meta && <Typography className={classes.meta}>{meta}</Typography>}
          {res.config_error && (
            <Typography className={classes.warn}>{res.config_error}</Typography>
          )}
          {res.ok === false && res.error && !checks.length && (
            <Typography className={classes.warn}>
              Monitoring is unavailable: {res.error}
            </Typography>
          )}
          {checks.length > 0 && !measured.length && (
            <Typography className={classes.warn}>{NOTHING_MEASURED}</Typography>
          )}
          {measured.map(row)}
          {unmeasured.length > 0 && (
            <details className={classes.fold}>
              <summary className={classes.foldSummary}>
                {unmeasuredText(unmeasured.length)}
              </summary>
              {unmeasured.map(row)}
            </details>
          )}
        </>
      )}
      {props.children}
    </InfoCard>
  );
}

function CheckRow(props: {
  check: MonitorCheck;
  apiBase: string;
  llm: boolean;
  busy: boolean;
  onAsk: (text: string) => void;
}) {
  const classes = useStyles();
  const { check: c, apiBase, llm, busy, onAsk } = props;
  const st = CHECK_STATE[c.state as CheckState] || CHECK_STATE.unknown;
  const toneClass = {
    red: classes.toneRed,
    amber: classes.toneAmber,
    emerald: classes.toneEmerald,
    faint: classes.toneFaint,
  }[st.tone];
  const watch = c.state === 'ok' ? watchingText(c) : '';
  const series = c.series || [];
  const count = typeof c.count === 'number' ? c.count : 0;
  // The alert is Cloud Monitoring's to run — it evaluates every minute whether or
  // not this portal is up. Shown for someone with rights in the project to apply;
  // the portal changes nothing.
  const [policy, setPolicy] = useState<{
    loading: boolean;
    text?: string;
    error?: string;
  } | null>(null);

  const togglePolicy = async () => {
    if (policy) {
      setPolicy(null);
      return;
    }
    setPolicy({ loading: true });
    let out: { ok?: boolean; error?: string; policy?: unknown };
    try {
      out = await getJson(
        apiBase,
        `/api/monitoring/alert-policy?${new URLSearchParams({ name: c.name })}`,
      );
    } catch (e) {
      out = { ok: false, error: String(e) };
    }
    if (!out || !out.ok)
      setPolicy({ loading: false, error: (out && out.error) || 'failed' });
    else
      setPolicy({ loading: false, text: JSON.stringify(out.policy, null, 2) });
  };

  return (
    <div className={classes.check} data-testid="monitor-check">
      <div className={classes.checkHead}>
        <span className={toneClass}>
          {st.text}
          {c.state === 'firing' ? ` (${c.count})` : ''}
        </span>
        <span className={classes.checkName}>{c.name}</span>
        {c.severity === 'warn' && (
          <span className={classes.severity}>warn</span>
        )}
        {watch && <span className={classes.muted}>{watch}</span>}
        <span className={classes.spacer} />
        {(c.state === 'firing' || c.state === 'unknown') && llm && (
          <Button
            size="small"
            color="primary"
            className={classes.ask}
            disabled={busy}
            onClick={() => onAsk(checkExplainPrompt(c))}
          >
            Ask why
          </Button>
        )}
        {c.state !== 'no_data' && (
          <Button
            size="small"
            className={classes.ask}
            title="The Cloud Monitoring alert policy that notifies you when this fires"
            onClick={togglePolicy}
          >
            Make it an alert
          </Button>
        )}
      </div>
      {c.description && (
        <Typography className={classes.muted}>{c.description}</Typography>
      )}
      <div className={classes.query} title={c.query}>
        {c.query}
      </div>
      {c.state === 'unknown' && (
        <Typography className={classes.warn} style={{ marginBottom: 0 }}>
          {c.error || 'no answer'}
          {c.hint ? ` — ${c.hint}` : ''}
        </Typography>
      )}
      {series.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table className={classes.series}>
            <tbody>
              {series.map((s, i) => (
                <tr key={i}>
                  <td className={classes.seriesLabel}>
                    {seriesLabel(s.labels)}
                  </td>
                  <td className={classes.seriesValue}>
                    {formatValue(s.value)}
                  </td>
                </tr>
              ))}
              {count > series.length && (
                <tr>
                  <td className={classes.muted} colSpan={2}>
                    …and {count - series.length} more
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {policy && (
        <Fragment>
          {policy.loading && (
            <Typography className={classes.muted}>
              Building the policy…
            </Typography>
          )}
          {policy.error && (
            <Typography className={classes.warn}>{policy.error}</Typography>
          )}
          {policy.text && (
            <>
              <Typography className={classes.muted} style={{ marginTop: 4 }}>
                {ALERT_POLICY_LEAD}
              </Typography>
              <pre className={classes.policy}>
                <code>{policy.text}</code>
              </pre>
              <div className={classes.command}>{ALERT_POLICY_COMMAND}</div>
            </>
          )}
        </Fragment>
      )}
    </div>
  );
}

// ---- the tab ---------------------------------------------------------------------

export function MonitoringTab(props: {
  onSend: (text: string) => Promise<void> | void;
  /** Any turn is in flight (one at a time, across tabs). */
  busy?: boolean;
  /** The reply to THIS tab's latest "Ask why" — shown under the card that asked. */
  result?: TurnReply;
  /** A model is configured (LLM_ENABLED): without one there is no "Ask why". */
  llm: boolean;
}) {
  const {
    onSend,
    busy = false,
    result = { text: '', streaming: false, pendingToken: null },
    llm,
  } = props;
  const apiBase = useApiBase();
  const [asked, setAsked] = useState<'bq' | 'checks' | null>(null);

  const ask = (from: 'bq' | 'checks') => (text: string) => {
    setAsked(from);
    return onSend(text);
  };

  // Monitoring never raises a CONFIRM token, so there is nothing to confirm.
  const reply = (
    <TurnResult {...result} onConfirm={() => {}} onCancel={() => {}} />
  );

  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <BqCostCard apiBase={apiBase} llm={llm} busy={busy} onAsk={ask('bq')}>
          {asked === 'bq' && reply}
        </BqCostCard>
      </Grid>
      <Grid item xs={12}>
        <ChecksCard
          apiBase={apiBase}
          llm={llm}
          busy={busy}
          onAsk={ask('checks')}
        >
          {asked === 'checks' && reply}
        </ChecksCard>
      </Grid>
    </Grid>
  );
}
