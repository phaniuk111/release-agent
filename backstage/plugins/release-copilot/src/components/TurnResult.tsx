import { Box, Button, makeStyles, Typography } from '@material-ui/core';
import { DEV_PORTAL as P } from '../look';
import { AgentMarkdown } from './AgentMarkdown';

/**
 * The reply to a form tab's own submission, shown in that tab: the preview as
 * it streams, then — when the agent asks for its CONFIRM token — Confirm and
 * Cancel right under it, then the outcome (PRs raised, run links). The same
 * flow the portal shows in one place; before this, Deploy showed only a small
 * box and Dataflow sent people to the Chat tab to find the token.
 */
const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  return {
    panel: {
      marginTop: theme.spacing(2),
      padding: theme.spacing(1.5, 2),
      borderRadius: P.radius.card,
      border: `1px solid ${theme.palette.divider}`,
      background: dark ? P.raised : 'rgba(15,23,42,.03)',
    },
    label: {
      fontSize: '0.75rem',
      fontWeight: 600,
      letterSpacing: '0.04em',
      textTransform: 'uppercase' as const,
      color: theme.palette.text.secondary,
      marginBottom: theme.spacing(1),
    },
    body: { maxHeight: 420, overflowY: 'auto' as const },
    streaming: { whiteSpace: 'pre-wrap' as const, fontSize: '0.875rem', lineHeight: 1.6 },
    confirm: {
      display: 'flex',
      flexWrap: 'wrap' as const,
      alignItems: 'center',
      gap: theme.spacing(1.5),
      marginTop: theme.spacing(1.5),
      padding: theme.spacing(1.25, 1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${P.amberBorder}`,
      background: dark ? P.amberSurface : 'rgba(245, 158, 11, 0.08)',
    },
    token: { fontFamily: P.mono, fontWeight: 700, color: dark ? P.amber : '#b45309' },
    spacer: { flex: 1 },
  };
});

export function TurnResult(props: {
  text: string;
  streaming: boolean;
  pendingToken: string | null;
  onConfirm: () => void;
  onCancel: () => void;
  confirmLabel?: string;
}) {
  const classes = useStyles();
  const { text, streaming, pendingToken, onConfirm, onCancel } = props;
  if (!text && !streaming) return null;
  return (
    <Box className={classes.panel} data-testid="turn-result">
      <Typography className={classes.label}>
        {streaming ? 'Agent is replying…' : 'Result'}
      </Typography>
      <div className={classes.body}>
        {streaming && (
          <span className={classes.streaming}>{text || 'Preparing the preview…'}▌</span>
        )}
        {!streaming && <AgentMarkdown text={text} />}
      </div>
      {pendingToken && !streaming && (
        <div className={classes.confirm} data-testid="inline-confirm">
          <Typography variant="body2">
            Preview ready — nothing changes until you confirm. Token{' '}
            <span className={classes.token}>{pendingToken}</span>
          </Typography>
          <span className={classes.spacer} />
          <Button variant="contained" color="primary" onClick={onConfirm}>
            {props.confirmLabel ?? 'Confirm & deploy'}
          </Button>
          <Button variant="outlined" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}
    </Box>
  );
}
