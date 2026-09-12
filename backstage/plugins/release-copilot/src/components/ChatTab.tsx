import { useCallback, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  Chip,
  Grid,
  IconButton,
  makeStyles,
  TextField,
  Typography,
} from '@material-ui/core';
import SendIcon from '@material-ui/icons/Send';
import RefreshIcon from '@material-ui/icons/Refresh';
import { Progress } from '@backstage/core-components';
import { apiGet, useApiBase } from '../api';
import type { ChatMessage } from './ReleaseCopilotPage';
import { AgentMarkdown } from './AgentMarkdown';
import { DEV_PORTAL as P } from '../look';

// The portal's pills that have no tab of their own here. Each sends the same
// text the portal pill does, so both front doors reach the same skill.
const QUICK_ASKS = [
  {
    label: 'Check release queue',
    hint: "What's queued for the next release — who added it, routing, JIRA, build status",
    text: "what's queued for the next release?",
  },
  {
    label: 'Consumer onboarding',
    hint: 'How to start using our APIs — access, auth, first call, going live',
    text: 'I want to onboard to your APIs — walk me through it step by step',
  },
];

// The portal's chat: your turns in the emerald→teal gradient on the right,
// the agent's on a raised glass surface on the left, notes muted in between.
const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  return {
    quickAsks: {
      display: 'flex',
      flexWrap: 'wrap' as const,
      gap: theme.spacing(1),
      margin: theme.spacing(1.5, 0, 1),
    },
    chatLog: {
      minHeight: 300,
      maxHeight: 460,
      overflowY: 'auto' as const,
      display: 'flex',
      flexDirection: 'column' as const,
      gap: theme.spacing(1.5),
      padding: theme.spacing(2),
      borderRadius: P.radius.card,
      border: `1px solid ${theme.palette.divider}`,
      background: dark ? 'rgba(2, 6, 23, 0.35)' : 'rgba(15, 23, 42, 0.03)',
      scrollBehavior: 'smooth' as const,
      '&::-webkit-scrollbar': { width: 8 },
      '&::-webkit-scrollbar-thumb': { background: 'rgba(148,163,184,.18)', borderRadius: 99 },
    },
    bubble: {
      maxWidth: '84%',
      padding: theme.spacing(1.25, 2),
      borderRadius: P.radius.card,
      lineHeight: 1.6,
      fontSize: '0.875rem',
      wordBreak: 'break-word' as const,
      animation: '$rise .28s cubic-bezier(.2,.8,.2,1)',
    },
    userMsg: {
      alignSelf: 'flex-end',
      whiteSpace: 'pre-wrap' as const,
      background: P.gradient,
      color: P.ink,
      fontWeight: 500,
    },
    agentMsg: {
      alignSelf: 'flex-start',
      background: dark ? P.raised : '#ffffff',
      border: `1px solid ${dark ? 'rgba(148,163,184,.10)' : theme.palette.divider}`,
      color: theme.palette.text.primary,
      '& code': {
        fontFamily: P.mono,
        background: dark ? P.sunken : 'rgba(15,23,42,.06)',
        color: dark ? P.mint : '#047857',
        padding: '1px 5px',
        borderRadius: 6,
      },
    },
    sysMsg: {
      alignSelf: 'center',
      whiteSpace: 'pre-wrap' as const,
      color: theme.palette.text.secondary,
      fontStyle: 'italic',
      fontSize: '0.8rem',
    },
    // The portal's "thinking" dots, while a reply has not started streaming.
    dots: {
      display: 'inline-flex',
      gap: 4,
      padding: theme.spacing(0.5, 0),
      '& span': {
        width: 6,
        height: 6,
        borderRadius: 99,
        background: P.textFaint,
        animation: '$blink 1.2s infinite',
      },
      '& span:nth-child(2)': { animationDelay: '.2s' },
      '& span:nth-child(3)': { animationDelay: '.4s' },
    },
    '@keyframes blink': {
      '0%, 80%, 100%': { opacity: 0.25, transform: 'translateY(0)' },
      '40%': { opacity: 1, transform: 'translateY(-3px)' },
    },
    '@keyframes rise': {
      from: { opacity: 0, transform: 'translateY(7px)' },
      to: { opacity: 1, transform: 'none' },
    },
    inputRow: {
      display: 'flex',
      gap: theme.spacing(1),
      marginTop: theme.spacing(1),
    },
    statusJson: {
      overflowX: 'auto' as const,
      fontSize: '0.78rem',
      fontFamily: P.mono,
      margin: 0,
      padding: theme.spacing(1.5),
      borderRadius: P.radius.control,
      background: dark ? P.sunken : 'rgba(15,23,42,.04)',
      color: dark ? P.mint : '#047857',
    },
  };
});

export function ChatTab(props: {
  messages: ChatMessage[];
  busy: boolean;
  onSend: (text: string) => Promise<void>;
}) {
  const classes = useStyles();
  const { messages, busy, onSend } = props;
  const [input, setInput] = useState('');

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput('');
    await onSend(text);
  }, [input, busy, onSend]);

  return (
    <Card>
      <CardHeader
        title="Chat"
        subheader="Talk to the ADK release agent — deploy previews and CONFIRM tokens appear here"
      />
      <CardContent>
        <div className={classes.chatLog}>
          {messages.length === 0 && (
            <Typography className={classes.sysMsg}>
              Try: "what is the current release status?" — or use the Deploy /
              Dataflow tabs; their submissions land here.
            </Typography>
          )}
          {messages.map((m, i) => {
            const msgClass =
              (
                { user: classes.userMsg, agent: classes.agentMsg } as Record<
                  string,
                  string
                >
              )[m.role] ?? classes.sysMsg;
            return (
              <div
                key={i}
                className={
                  m.role === 'system' ? msgClass : `${classes.bubble} ${msgClass}`
                }
              >
                {m.role !== 'agent' && m.text}
                {m.role === 'agent' && m.text && <AgentMarkdown text={m.text} />}
                {m.role === 'agent' && !m.text && (
                  <span className={classes.dots} aria-label="The agent is replying">
                    <span />
                    <span />
                    <span />
                  </span>
                )}
              </div>
            );
          })}
        </div>
        <div className={classes.quickAsks}>
          {QUICK_ASKS.map(q => (
            <Chip
              key={q.label}
              label={q.label}
              title={q.hint}
              size="small"
              variant="outlined"
              clickable
              disabled={busy}
              onClick={() => onSend(q.text)}
            />
          ))}
        </div>
        <div className={classes.inputRow}>
          <TextField
            fullWidth
            variant="outlined"
            size="small"
            placeholder="Message the release agent…"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') send();
            }}
            disabled={busy}
          />
          <Button
            variant="contained"
            color="primary"
            endIcon={<SendIcon />}
            onClick={send}
            disabled={busy}
          >
            Send
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function StatusCard() {
  const classes = useStyles();
  const apiBase = useApiBase();
  const [status, setStatus] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await apiGet(apiBase, '/api/release-status'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  return (
    <Card>
      <CardHeader
        title="Release status"
        action={
          <IconButton onClick={refresh} disabled={loading} size="small">
            <RefreshIcon />
          </IconButton>
        }
      />
      <CardContent>
        {loading && <Progress />}
        {error && <Typography color="error">{error}</Typography>}
        {!status && !loading && !error && (
          <Typography color="textSecondary">
            Click refresh to load the current release status.
          </Typography>
        )}
        {status !== null && (
          <pre className={classes.statusJson}>
            {JSON.stringify(status, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}

export function ChatGrid(props: {
  messages: ChatMessage[];
  busy: boolean;
  onSend: (text: string) => Promise<void>;
}) {
  return (
    <Grid container spacing={3}>
      <Grid item xs={12} md={7}>
        <ChatTab {...props} />
      </Grid>
      <Grid item xs={12} md={5}>
        <StatusCard />
      </Grid>
    </Grid>
  );
}
