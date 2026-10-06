import { useState } from 'react';
import { Box, Button, Chip, Collapse, makeStyles, Typography } from '@material-ui/core';
import { DEV_PORTAL as P } from '../look';
import { AgentMarkdown } from './AgentMarkdown';

const useStyles = makeStyles(theme => ({
  // Sticky under the page header: a preview can be long, the decision must stay in view.
  banner: {
    position: 'sticky',
    top: 0,
    zIndex: 5,
    marginBottom: theme.spacing(2),
    padding: theme.spacing(1.5, 2),
    borderRadius: P.radius.card,
    border: `1px solid ${P.amberBorder}`,
    background: theme.palette.type === 'light' ? 'rgba(255, 251, 235, 0.97)' : 'rgba(30, 24, 10, 0.92)',
    backdropFilter: 'blur(10px)',
    boxShadow: '0 8px 24px rgba(0, 0, 0, 0.25)',
  },
  row: { display: 'flex', alignItems: 'center', gap: theme.spacing(1.5), flexWrap: 'wrap' },
  icon: { fontSize: '1.25rem', lineHeight: 1 },
  what: { fontWeight: 600 },
  summary: { opacity: 0.85, fontSize: '0.85rem' },
  token: { fontFamily: P.mono, fontWeight: 700, color: P.amber, borderColor: P.amberBorder },
  spacer: { flex: 1 },
  toggle: { textTransform: 'none', fontSize: '0.8rem' },
  detail: {
    marginTop: theme.spacing(1.5),
    maxHeight: '45vh',
    overflow: 'auto',
    paddingTop: theme.spacing(1),
    borderTop: `1px solid ${P.amberBorder}`,
  },
  steps: { fontSize: '0.75rem', opacity: 0.75, marginTop: theme.spacing(0.5) },
}));

const MARKS = new Set(['*', '_', '`', '#', '>']);

/** The first readable line of a preview: no markdown marks, no code fences. */
export function summaryOf(text: string): string {
  for (const raw of (text || '').split('\n')) {
    const line = Array.from(raw)
      .filter(ch => !MARKS.has(ch))
      .join('')
      .trim();
    if (line && !line.startsWith('{') && !line.startsWith('```')) return line.length > 140 ? `${line.slice(0, 139)}…` : line;
  }
  return '';
}

export type BannerProps = {
  /** What is waiting: "Deploy to CARE UAT", "CARE / DF release", "Promotion" … */
  what: string;
  /** A CONFIRM token to apply, or null for a yes/no approval. */
  token: string | null;
  /** The agent's preview (or question), shown on demand. */
  detail: string;
  /** Still being built: show the steps, hide the buttons. */
  building?: boolean;
  /** What is happening after an answer: "Deploying to CARE UAT…", "Cancelling — nothing will change…". */
  applying?: string | null;
  steps?: string[];
  onConfirm: () => void;
  onCancel: () => void;
};

/**
 * The one place a decision is made. A deploy or release preview (CONFIRM
 * token) and a yes/no approval (a promotion) both land here, at the top of
 * Ship and Queue, wherever they were raised — the form below keeps its fields
 * and, once answered, the outcome.
 */
export function ActionBanner(props: BannerProps) {
  const classes = useStyles();
  const [open, setOpen] = useState(false);
  const { what, token, detail, applying, steps = [] } = props;
  const building = props.building || applying;
  const approval = !token;
  return (
    <Box className={classes.banner} data-testid="action-banner" role="region" aria-label={`${what} — waiting for you`}>
      <div className={classes.row}>
        <span className={classes.icon} aria-hidden>
          {building ? '⏳' : approval ? '🛡️' : '🚀'}
        </span>
        <Box>
          <Typography className={classes.what}>
            {applying
              ? applying
              : building
                ? `${what} — building the preview…`
                : approval
                  ? `${what} — approval needed`
                  : `${what} — ready to confirm`}
          </Typography>
          {!building && summaryOf(detail) && (
            <Typography className={classes.summary}>{summaryOf(detail)}</Typography>
          )}
          {building && steps.length > 0 && (
            <div className={classes.steps}>{steps[steps.length - 1]}…</div>
          )}
        </Box>
        <span className={classes.spacer} />
        {token && !building && <Chip variant="outlined" size="small" label={token} className={classes.token} />}
        {!building && detail && (
          <Button className={classes.toggle} size="small" onClick={() => setOpen(o => !o)}>
            {open ? 'Hide preview' : 'Show preview'}
          </Button>
        )}
        {!building && (
          <>
            <Button variant="contained" color="primary" onClick={props.onConfirm}>
              {approval ? 'Approve' : 'Confirm'}
            </Button>
            <Button variant="outlined" onClick={props.onCancel}>
              {approval ? 'Reject' : 'Cancel'}
            </Button>
          </>
        )}
      </div>
      <Collapse in={open && !building}>
        <div className={classes.detail}>
          <AgentMarkdown text={detail} />
        </div>
      </Collapse>
    </Box>
  );
}
