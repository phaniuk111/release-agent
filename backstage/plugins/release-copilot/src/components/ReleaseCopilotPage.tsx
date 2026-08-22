import { useCallback, useState } from 'react';
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

const TABS = ['Chat', 'Deploy', 'Dataflow', 'Releases', 'Queue', 'Insights'] as const;

export function ReleaseCopilotPage() {
  const classes = useStyles();
  const [tab, setTab] = useState(0);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [threadId] = useState<string | null>(null);
  const apiBase = useApiBase();

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
          } else if (ev.type === 'error') {
            append({
              role: 'system',
              text: `Error: ${ev.content ?? 'unknown'}`,
            });
          }
        });
      } catch (e) {
        append({ role: 'system', text: `Error: ${(e as Error).message}` });
      } finally {
        setBusy(false);
      }
    },
    [busy, threadId, append],
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
          onChange={(_, v) => setTab(v)}
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
