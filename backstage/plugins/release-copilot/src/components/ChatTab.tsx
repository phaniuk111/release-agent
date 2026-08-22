import { useCallback, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
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

const useStyles = makeStyles(theme => ({
  chatLog: {
    minHeight: 300,
    maxHeight: 460,
    overflowY: 'auto',
    padding: theme.spacing(1),
    background: theme.palette.background.default,
    borderRadius: 4,
    whiteSpace: 'pre-wrap',
    fontFamily: 'monospace',
    fontSize: '0.85rem',
  },
  userMsg: { color: theme.palette.primary.main },
  agentMsg: { color: theme.palette.text.primary },
  sysMsg: { color: theme.palette.text.secondary, fontStyle: 'italic' },
  inputRow: { display: 'flex', gap: theme.spacing(1), marginTop: theme.spacing(1) },
}));

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
          {messages.map((m, i) => (
            <div
              key={i}
              className={
                m.role === 'user'
                  ? classes.userMsg
                  : m.role === 'agent'
                  ? classes.agentMsg
                  : classes.sysMsg
              }
            >
              {m.role === 'user' ? '» ' : ''}
              {m.text}
            </div>
          ))}
          {busy && <Progress />}
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
        {status != null && (
          <pre style={{ overflowX: 'auto', fontSize: '0.8rem' }}>
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
