import { useCallback, useRef, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  Grid,
  IconButton,
  List,
  ListItem,
  ListItemText,
  makeStyles,
  TextField,
  Typography,
} from '@material-ui/core';
import SendIcon from '@material-ui/icons/Send';
import RefreshIcon from '@material-ui/icons/Refresh';
import { Content, Header, Page, Progress } from '@backstage/core-components';

/**
 * Base path of the release-copilot service, reached through Backstage's
 * built-in proxy backend (see app-config.yaml `proxy.endpoints`).
 */
const API_BASE = '/api/proxy/release-copilot';

type ChatEvent = {
  type: 'token' | 'progress' | 'interrupt' | 'confirmation' | 'done' | 'error';
  content?: string;
  data?: unknown;
  mutated?: boolean;
};

type ChatMessage = { role: 'user' | 'agent' | 'system'; text: string };

const useStyles = makeStyles(theme => ({
  chatLog: {
    minHeight: 280,
    maxHeight: 420,
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

/** Stream one chat turn through the SSE proxy, appending tokens as they arrive. */
async function streamChat(
  message: string,
  threadId: string | null,
  onEvent: (ev: ChatEvent) => void,
): Promise<void> {
  const resp = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, thread_id: threadId }),
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`chat failed: HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith('data:')) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
      } catch {
        /* ignore malformed frame */
      }
    }
  }
}

function ChatPanel() {
  const classes = useStyles();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const threadIdRef = useRef<string | null>(null);

  const append = useCallback((msg: ChatMessage) => {
    setMessages(prev => [...prev, msg]);
  }, []);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput('');
    setBusy(true);
    append({ role: 'user', text });
    append({ role: 'agent', text: '' });
    try {
      await streamChat(text, threadIdRef.current, ev => {
        if (ev.type === 'token' && ev.content) {
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            next[next.length - 1] = { ...last, text: last.text + ev.content };
            return next;
          });
        } else if (ev.type === 'interrupt') {
          append({
            role: 'system',
            text: '⚠ Confirmation required — reply with the exact CONFIRM token to proceed.',
          });
        } else if (ev.type === 'error') {
          append({ role: 'system', text: `Error: ${ev.content ?? 'unknown'}` });
        }
      });
    } catch (e) {
      append({ role: 'system', text: `Error: ${(e as Error).message}` });
    } finally {
      setBusy(false);
    }
  }, [input, busy, append]);

  return (
    <Card>
      <CardHeader title="Chat" subheader="Talk to the ADK release agent" />
      <CardContent>
        <div className={classes.chatLog}>
          {messages.length === 0 && (
            <Typography className={classes.sysMsg}>
              Try: "what is the current release status?" or "deploy
              my-chart:1.2.3 to uat"
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

function StatusPanel() {
  const [status, setStatus] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`${API_BASE}/api/release-status`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setStatus(await resp.json());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

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
          <pre style={{ overflowX: 'auto' }}>
            {JSON.stringify(status, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}

function QueuePanel() {
  const [rows, setRows] = useState<{ summary?: string }[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const resp = await fetch(`${API_BASE}/api/release-queue`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setRows(Array.isArray(data) ? data : data.rows ?? []);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  return (
    <Card>
      <CardHeader
        title="Release queue"
        action={
          <IconButton onClick={refresh} size="small">
            <RefreshIcon />
          </IconButton>
        }
      />
      <CardContent>
        {error && <Typography color="error">{error}</Typography>}
        {rows == null && !error && (
          <Typography color="textSecondary">
            Click refresh to load the intake queue.
          </Typography>
        )}
        {rows != null && rows.length === 0 && (
          <Typography color="textSecondary">Queue is empty.</Typography>
        )}
        {rows != null && rows.length > 0 && (
          <List dense>
            {rows.map((r, i) => (
              <ListItem key={i}>
                <ListItemText primary={r.summary ?? JSON.stringify(r)} />
              </ListItem>
            ))}
          </List>
        )}
      </CardContent>
    </Card>
  );
}

export function ReleaseCopilotPage() {
  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="ADK release agent, proxied through Backstage (PoC)"
      />
      <Content>
        <Grid container spacing={3}>
          <Grid item xs={12} md={7}>
            <ChatPanel />
          </Grid>
          <Grid item xs={12} md={5}>
            <Grid container spacing={3} direction="column">
              <Grid item>
                <StatusPanel />
              </Grid>
              <Grid item>
                <QueuePanel />
              </Grid>
            </Grid>
          </Grid>
        </Grid>
      </Content>
    </Page>
  );
}
