import { Link, makeStyles, Typography } from '@material-ui/core';
import { DEV_PORTAL as P } from '../look';

type Chart = { helm_chart_name?: string; helm_chart_version?: string };
type Pr = { number?: number; url?: string; charts?: Chart[]; head?: string; base?: string };

export type ReleaseStatus = {
  error?: string;
  date_utc?: string;
  now_utc?: string;
  reason?: string;
  prd_release_pr?: Pr | null;
  pending_to_prod?: unknown[];
  prd_charts?: Chart[];
  queued_next?: number;
  blocking_pr?: Pr | null;
  df?: { error?: string | null; prd_release_pr?: Pr | null } | null;
};

const useStyles = makeStyles(theme => ({
  title: { display: 'flex', alignItems: 'center', gap: theme.spacing(1), fontWeight: 600 },
  dot: { width: 8, height: 8, borderRadius: 99, flexShrink: 0 },
  line: { marginTop: theme.spacing(0.75), fontSize: '0.85rem', color: theme.palette.text.secondary },
  charts: { fontFamily: P.mono, fontSize: '0.78rem', color: theme.palette.text.primary },
  warn: { color: P.amber },
}));

const chartList = (charts?: Chart[]) =>
  (charts ?? []).map(c => `${c.helm_chart_name}:${c.helm_chart_version}`).join('  │  ');

/**
 * The portal banner's reading of /api/release-status: what is open, what is
 * staged, what blocks adds, how much is queued — instead of the raw JSON the
 * card used to print.
 */
export function StatusSummary(props: { status: ReleaseStatus }) {
  const classes = useStyles();
  const s = props.status;
  if (s.error) {
    return (
      <Typography className={classes.warn}>Couldn't fetch release status: {s.error}</Typography>
    );
  }
  const pr = s.prd_release_pr;
  const staged = (s.pending_to_prod ?? []).length;
  const df = s.df;
  let dfText = '';
  if (df?.error) dfText = 'DF: unavailable';
  else if (df?.prd_release_pr) dfText = `DF: PR #${df.prd_release_pr.number} open`;
  else if (df) dfText = 'DF: none open';
  const blk = s.blocking_pr;
  const title = [
    pr ? `CARE: PR #${pr.number} open · ${staged} change${staged === 1 ? '' : 's'} staged` : 'CARE: no release open',
    dfText,
    blk ? `⚠ adds blocked by PR #${blk.number}` : '',
    typeof s.queued_next === 'number' && s.queued_next > 0 ? `${s.queued_next} queued for next release` : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div>
      <Typography className={classes.title}>
        <span className={classes.dot} style={{ background: pr || blk ? P.amber : P.emerald }} />
        {title}
      </Typography>
      <Typography className={classes.line}>
        {s.reason ? `${s.reason} · ` : ''}as of {s.now_utc} UTC · {s.date_utc}
      </Typography>
      {pr && (
        <Typography className={classes.line}>
          {(pr.charts ?? []).length > 0 && (
            <>
              staged: <span className={classes.charts}>{chartList(pr.charts)}</span>{' '}
            </>
          )}
          <Link href={pr.url} target="_blank" rel="noopener noreferrer">
            open PR #{pr.number}
          </Link>
        </Typography>
      )}
      {(s.prd_charts ?? []).length > 0 && (
        <Typography className={classes.line}>
          PRD ({s.prd_charts!.length}): <span className={classes.charts}>{chartList(s.prd_charts)}</span>
        </Typography>
      )}
      {df?.error && (
        <Typography className={`${classes.line} ${classes.warn}`}>
          DF release status unavailable ({df.error})
        </Typography>
      )}
      {df?.prd_release_pr && (
        <Typography className={classes.line}>
          DF release:{' '}
          <Link href={df.prd_release_pr.url} target="_blank" rel="noopener noreferrer">
            PR #{df.prd_release_pr.number}
          </Link>
          {(df.prd_release_pr.charts ?? []).length > 0 && (
            <> — staged: <span className={classes.charts}>{chartList(df.prd_release_pr.charts)}</span></>
          )}
        </Typography>
      )}
      {blk && (
        <Typography className={`${classes.line} ${classes.warn}`}>
          ⚠ Adds to the release are blocked:{' '}
          <Link href={blk.url} target="_blank" rel="noopener noreferrer">
            PR #{blk.number}
          </Link>{' '}
          ({blk.head} → {blk.base}) is open — merge or close it first.
        </Typography>
      )}
    </div>
  );
}
