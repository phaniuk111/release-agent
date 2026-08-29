import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { useApi } from '@backstage/core-plugin-api';
import { Box, makeStyles, Tab, Tabs } from '@material-ui/core';
import { Content, Header, Page } from '@backstage/core-components';
import { streamChat, useApiBase } from '../api';
import { ChatGrid } from './ChatTab';
import { DeployTab } from './DeployTab';
import { DataflowTab } from './DataflowTab';
import { ReleasesTab } from './ReleasesTab';
import { QueueTab } from './QueueTab';
import { InsightsTab } from './InsightsTab';

export type ChatMessage = { role: 'user' | 'agent' | 'system'; text: string };

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
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

  const append = useCallback((msg: ChatMessage) => {
    setMessages(prev => [...prev, msg]);
  }, []);

  /** Shared chat turn used by the Chat tab AND the form tabs. */
  const send = useCallback(
    async (text: string) => {
      if (busy) return;
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
            toastApi.post({
              title: 'Confirmation required',
              description:
                'Reply in Chat with the exact CONFIRM token to proceed.',
              status: 'warning',
              timeout: 6000,
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
        // Stream completed without error — show a transient success toast for
        // form-tab submissions (Deploy/Dataflow pass their text here).
        toastApi.post({
          title: 'Request sent',
          status: 'success',
          timeout: 3000,
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

  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="ADK release agent, proxied through Backstage (PoC)"
      />
      <Content>
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
          {tab === 1 && <DeployTab onSend={send} />}
          {tab === 2 && <DataflowTab onSend={send} />}
          {tab === 3 && <ReleasesTab />}
          {tab === 4 && <QueueTab />}
          {tab === 5 && <InsightsTab />}
        </Box>
      </Content>
    </Page>
  );
}
