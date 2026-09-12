import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Box, Button, makeStyles, Tab, Tabs, Typography } from '@material-ui/core';
import { Content, Header, Page } from '@backstage/core-components';
import { ChatGrid } from './ChatTab';
import { DeployTab } from './DeployTab';
import { DataflowTab } from './DataflowTab';
import { ReleasesTab } from './ReleasesTab';
import { QueueTab } from './QueueTab';
import { InsightsTab } from './InsightsTab';
import { useAgentChat } from './useAgentChat';
import { DEV_PORTAL as P } from '../look';

export type { ChatMessage } from './useAgentChat';

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
  // The portal's confirmation box: amber, on glass.
  confirmBar: {
    display: 'flex',
    alignItems: 'center',
    gap: theme.spacing(1.5),
    padding: theme.spacing(1.5, 2),
    marginBottom: theme.spacing(2),
    borderRadius: P.radius.card,
    border: `1px solid ${P.amberBorder}`,
    background:
      theme.palette.type === 'light' ? 'rgba(245, 158, 11, 0.08)' : P.amberSurface,
    backdropFilter: 'blur(10px)',
  },
  confirmToken: {
    fontFamily: P.mono,
    fontWeight: 700,
    color: P.amber,
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

// Tabs whose submissions show their own result (and confirm) in place.
const ORIGIN_OF_TAB: Record<string, string> = { Deploy: 'deploy', Dataflow: 'dataflow' };

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

  const chat = useAgentChat('release');
  const deploy = chat.resultFor('deploy');
  const dataflow = chat.resultFor('dataflow');
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });

  // The page-level bar is for a token the current tab cannot confirm itself:
  // one raised in Chat, or one raised by a form tab you have since left.
  const showBar =
    chat.pending && !chat.busy && ORIGIN_OF_TAB[TABS[tab]] !== chat.pending.origin;

  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="ADK release agent, proxied through Backstage (PoC)"
      />
      <Content>
        {showBar && chat.pending && (
          <Box className={classes.confirmBar} data-testid="confirm-bar">
            <Typography>
              Preview ready — confirm to release. Token:{' '}
              <span className={classes.confirmToken}>{chat.pending.token}</span>
            </Typography>
            <span className={classes.spacer} />
            <Button
              variant="contained"
              color="primary"
              onClick={chat.confirm}
              startIcon={<span>🚀</span>}
            >
              Confirm &amp; release
            </Button>
            <Button variant="outlined" onClick={chat.dismiss}>
              Cancel
            </Button>
          </Box>
        )}
        <Tabs
          className={classes.tabsBar}
          value={tab}
          onChange={(_, v) => selectTab(v)}
          indicatorColor="primary"
          // On a narrow screen the six tabs overflow; scroll them rather than
          // cutting "Insights" off where nobody can reach it.
          variant="scrollable"
          scrollButtons="auto"
        >
          {TABS.map(label => (
            <Tab key={label} label={label} />
          ))}
        </Tabs>
        <Box mt={3}>
          {tab === 0 && (
            <ChatGrid messages={chat.messages} busy={chat.busy} onSend={sendFrom('chat')} />
          )}
          {tab === 1 && (
            <DeployTab
              onSend={sendFrom('deploy')}
              busy={chat.busy}
              result={deploy}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {tab === 2 && (
            <DataflowTab
              onSend={sendFrom('dataflow')}
              busy={chat.busy}
              result={dataflow}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {tab === 3 && <ReleasesTab />}
          {tab === 4 && <QueueTab />}
          {tab === 5 && <InsightsTab />}
        </Box>
      </Content>
    </Page>
  );
}
