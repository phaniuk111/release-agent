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
import { HistoryTab } from './HistoryTab';
import { SupportTab } from './SupportTab';
import { MonitoringTab } from './MonitoringTab';
import { useAgentChat } from './useAgentChat';
import { useUiConfig } from '../api';
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

const ALL_TABS = [
  'Chat',
  'Deploy',
  'Dataflow',
  'Releases',
  'Queue',
  'History',
  'Support',
  'Monitoring',
  'Insights',
] as const;
type TabName = (typeof ALL_TABS)[number];

// The portal's pill group a tab belongs to, when that group can be preview
// (PREVIEW_GROUPS): a caller outside the preview does not see the tab, and the
// agent refuses its API anyway.
const GROUP_OF_TAB: Partial<Record<TabName, string>> = { Monitoring: 'Monitoring', Support: 'Support' };

// Tabs whose submissions show their own result (and confirm) in place.
const ORIGIN_OF_TAB: Partial<Record<TabName, string>> = {
  Deploy: 'deploy',
  Dataflow: 'dataflow',
  Support: 'support',
  Monitoring: 'monitoring',
};

export function ReleaseCopilotPage() {
  const classes = useStyles();
  const ui = useUiConfig();
  const TABS = ALL_TABS.filter(t => {
    const group = GROUP_OF_TAB[t];
    return !group || !ui.hiddenGroups.includes(group);
  });
  const [searchParams, setSearchParams] = useSearchParams();
  const tabName = (searchParams.get('tab') ?? '').toLowerCase();
  const urlTab = TABS.map(t => t.toLowerCase()).indexOf(tabName);
  const [tab, setTab] = useState(urlTab >= 0 ? urlTab : 0);
  const current: TabName = TABS[Math.min(tab, TABS.length - 1)];

  // Deep-link: /release-copilot?tab=queue (or deploy, insights, ...)
  useEffect(() => {
    if (urlTab >= 0 && urlTab !== tab) setTab(urlTab);
  }, [urlTab]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectTab = useCallback(
    (v: number) => {
      setTab(v);
      setSearchParams({ tab: TABS[v].toLowerCase() }, { replace: true });
    },
    [setSearchParams, TABS],
  );

  const chat = useAgentChat('release');
  const deploy = chat.resultFor('deploy');
  const dataflow = chat.resultFor('dataflow');
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });

  // The page-level bar is for a token the current tab cannot confirm itself:
  // one raised in Chat, or one raised by a form tab you have since left.
  const showBar =
    chat.pending && !chat.busy && ORIGIN_OF_TAB[current] !== chat.pending.origin;

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
        {chat.approval && !chat.busy && (
          <Box className={classes.confirmBar} data-testid="approval-bar">
            <Typography>
              <strong>Approval required</strong> — {chat.approval.message.replace(/\*\*/g, '')}
            </Typography>
            <span className={classes.spacer} />
            <Button variant="contained" color="primary" onClick={chat.approve}>
              Approve
            </Button>
            <Button variant="outlined" onClick={chat.reject}>
              Reject
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
          {current === 'Chat' && (
            <ChatGrid messages={chat.messages} busy={chat.busy} onSend={sendFrom('chat')} />
          )}
          {current === 'Deploy' && (
            <DeployTab
              onSend={sendFrom('deploy')}
              busy={chat.busy}
              result={deploy}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'Dataflow' && (
            <DataflowTab
              onSend={sendFrom('dataflow')}
              busy={chat.busy}
              result={dataflow}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'Releases' && <ReleasesTab />}
          {current === 'Queue' && <QueueTab />}
          {current === 'History' && (
            <HistoryTab onOpenQueue={() => selectTab(TABS.indexOf('Queue'))} />
          )}
          {current === 'Support' && (
            <SupportTab
              onSend={sendFrom('support')}
              busy={chat.busy}
              result={chat.resultFor('support')}
              llm={ui.llm}
              investigation={chat.investigationFor('support')}
            />
          )}
          {current === 'Monitoring' && (
            <MonitoringTab
              onSend={sendFrom('monitoring')}
              busy={chat.busy}
              result={chat.resultFor('monitoring')}
              llm={ui.llm}
            />
          )}
          {current === 'Insights' && <InsightsTab />}
        </Box>
      </Content>
    </Page>
  );
}
