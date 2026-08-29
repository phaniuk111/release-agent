import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { useApi } from '@backstage/core-plugin-api';
import { Box, Button, makeStyles, Tab, Tabs, Typography } from '@material-ui/core';
import { Content, Header, Page } from '@backstage/core-components';
import { streamChat, useApiBase } from '../api';
import { ChatGrid } from './ChatTab';
import { DeployTab } from './DeployTab';
import { DataflowTab } from './DataflowTab';
import { ReleasesTab } from './ReleasesTab';
import { QueueTab } from './QueueTab';
import { InsightsTab } from './InsightsTab';

export type ChatMessage = { role: 'user' | 'agent' | 'system'; text: string };

const CONFIRM_TOKEN_RE = /CONFIRM-[A-F0-9]{4,10}\b/i;

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
  confirmBar: {
    display: 'flex',
    alignItems: 'center',
    gap: theme.spacing(1.5),
    padding: theme.spacing(1.5, 2),
    marginBottom: theme.spacing(2),
    borderRadius: theme.shape.borderRadius,
    border: `1px solid ${theme.palette.warning.main}`,
    background:
      theme.palette.type === 'light'
        ? 'rgba(255, 152, 0, 0.08)'
        : 'rgba(255, 152, 0, 0.12)',
  },
  confirmToken: {
    fontFamily: 'monospace',
    fontWeight: 700,
  },
  spacer: { flex: 1 },
}));

const TABS = [
  'Chat',
  'Deploy',
  'Dataflow',
  'Releases',
  'Queue',
  'Insights',
] as const;

export function ReleaseCopilotPage() {
  const classes = useStyles();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabName = (searchParams.get('tab') ?? '').toLowerCase();
  const urlTab = TABS.map(t => t.toLowerCase()).indexOf(tabName);
  const [tab, setTab] = useState(urlTab >= 0 ? urlTab : 0);

  // Deep-link: /release-copilot?tab=queue (or deploy, insights, ...)
  useEffect(() => {
    if (urlTab >= 0 && urlTab !== tab) setTab(urlTab);
  }, [urlTab]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectTab = useCallback(
    (v: number) => {
      setTab(v);
      setSearchParams({ tab: TABS[v].toLowerCase() }, { replace: true });
    },
    [setSearchParams],
  );
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [threadId] = useState<string | null>(null);
  const apiBase = useApiBase();
  const toastApi = useApi(toastApiRef);

  // When the agent ends a turn with a confirmation prompt, surface the token
  // right where the user is (Deploy/Dataflow tabs) — no chat-tab round trip.
  const [pendingConfirm, setPendingConfirm] = useState<string | null>(null);

  const append = useCallback((msg: ChatMessage) => {
    setMessages(prev => [...prev, msg]);
  }, []);

  /** Shared chat turn used by the Chat tab AND the form tabs. */
  const send = useCallback(
    async (text: string) => {
      if (busy) return;
      setPendingConfirm(null);
      setBusy(true);
      append({ role: 'user', text });
      append({ role: 'agent', text: '' });
      try {
        await streamChat(apiBase, text, threadId, ev => {
          if (ev.type === 'token' && ev.content) {
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              next[next.length - 1] = {
                ...last,
                text: last.text + ev.content,
              };
              return next;
            });
          } else if (ev.type === 'interrupt') {
            append({
              role: 'system',
              text: '⚠ Confirmation required — reply with the exact CONFIRM token to proceed.',
            });
          } else if (ev.type === 'error') {
            append({
              role: 'system',
              text: `Error: ${ev.content ?? 'unknown'}`,
            });
            toastApi.post({
              title: 'Release Copilot error',
              description: String(ev.content ?? 'unknown error'),
              status: 'danger',
            });
          }
        });
        // If the agent ended its turn with a confirmation prompt, capture the
        // token so the current tab can confirm inline.
        setMessages(prev => {
          const last = [...prev].reverse().find(m => m.role === 'agent');
          const match = last?.text.match(CONFIRM_TOKEN_RE);
          if (match) setPendingConfirm(match[0].toUpperCase());
          return prev;
        });
      } catch (e) {
        append({ role: 'system', text: `Error: ${(e as Error).message}` });
        toastApi.post({
          title: 'Release Copilot request failed',
          description: (e as Error).message,
          status: 'danger',
        });
      } finally {
        setBusy(false);
      }
    },
    [busy, threadId, append, apiBase, toastApi],
  );

  const confirmAndRelease = useCallback(() => {
    if (pendingConfirm) send(pendingConfirm);
  }, [pendingConfirm, send]);

  const dismissConfirm = useCallback(() => {
    setPendingConfirm(null);
    append({
      role: 'system',
      text: '✖ Preview dismissed — nothing was deployed.',
    });
    toastApi.post({
      title: 'Not confirmed',
      description: 'The preview was dismissed — nothing was deployed.',
      status: 'info',
      timeout: 5000,
    });
  }, [append, toastApi]);

  const lastAgentText = useRef('');
  const msgs = messages;
  const lastAgent = [...msgs].reverse().find(m => m.role === 'agent');
  lastAgentText.current = lastAgent?.text ?? '';

  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="ADK release agent, proxied through Backstage (PoC)"
      />
      <Content>
        {pendingConfirm && !busy && (
          <Box className={classes.confirmBar} data-testid="confirm-bar">
            <Typography>
              Preview ready — confirm to release. Token:{' '}
              <span className={classes.confirmToken}>{pendingConfirm}</span>
            </Typography>
            <span className={classes.spacer} />
            <Button
              variant="contained"
              color="primary"
              onClick={confirmAndRelease}
              startIcon={<span>🚀</span>}
            >
              Confirm &amp; release
            </Button>
            <Button variant="outlined" onClick={dismissConfirm}>
              Cancel
            </Button>
          </Box>
        )}
        <Tabs
          className={classes.tabsBar}
          value={tab}
          onChange={(_, v) => selectTab(v)}
          indicatorColor="primary"
        >
          {TABS.map(label => (
            <Tab key={label} label={label} />
          ))}
        </Tabs>
        <Box mt={3}>
          {tab === 0 && (
            <ChatGrid messages={messages} busy={busy} onSend={send} />
          )}
          {tab === 1 && (
            <DeployTab
              onSend={send}
              busy={busy}
              agentResponse={lastAgentText.current}
              pendingConfirm={pendingConfirm}
            />
          )}
          {tab === 2 && <DataflowTab onSend={send} />}
          {tab === 3 && <ReleasesTab />}
          {tab === 4 && <QueueTab />}
          {tab === 5 && <InsightsTab />}
        </Box>
      </Content>
    </Page>
  );
}