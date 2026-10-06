import { useCallback, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useRouteRef } from '@backstage/frontend-plugin-api';
import { operationsRouteRef } from '../plugin';
import { Box, Chip, makeStyles, Tab, Tabs } from '@material-ui/core';
import { Content, Header, HeaderLabel, Page } from '@backstage/core-components';
import { DeployTab } from './DeployTab';
import { DataflowTab } from './DataflowTab';
import { ReleasesTab } from './ReleasesTab';
import { PromoteTab } from './PromoteTab';
import { QueueTab } from './QueueTab';
import { HistoryTab } from './HistoryTab';
import { useAgentChat } from './useAgentChat';
import { ActionBanner, bannerFor } from './ActionBanner';
import { useSignedInAs, useUiConfig } from '../api';
import { VIEW_LABEL, View, areaOf, opsViewFor, resolve, visibleAreas } from './navigation';

export type { ChatMessage } from './useAgentChat';

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
  // The screens of the chosen area: a quiet row under the area tabs.
  views: { display: 'flex', flexWrap: 'wrap', gap: theme.spacing(1), marginTop: theme.spacing(2) },
}));

// How the banner names a decision, by the screen that raised it …
const WHAT_OF_ORIGIN: Record<string, string> = {
  deploy: 'Deploy to CARE UAT',
  dataflow: 'Deploy to DF UAT',
  releases: 'CARE / DF release',
  promote: 'Promotion',
};
// … and what it says while a confirmed decision is carried out.
const DOING_OF_ORIGIN: Record<string, string> = {
  deploy: 'Deploying to CARE UAT — merging into SIT…',
  dataflow: 'Dispatching the Dataflow deploy…',
  releases: 'Creating the release…',
  promote: 'Promoting — copying the release files and merging…',
};

/**
 * Release work: Ship (deploy to UAT, cut a release, promote it) and Queue (the
 * next release, and the history to put charts back from). Every decision — a
 * CONFIRM token or a yes/no approval — is answered in one banner at the top;
 * asking the agent about releases happens in Operations → Insights.
 */
export function ReleaseCopilotPage() {
  const classes = useStyles();
  const ui = useUiConfig();
  const signedInAs = useSignedInAs();
  const areas = visibleAreas(ui.hiddenGroups);
  const [searchParams, setSearchParams] = useSearchParams();
  const { area, view: current } = resolve(areas, searchParams.get('tab'), searchParams.get('view'));

  // Old links to what now lives in Operations — its screens, and the chat
  // (?tab=ask), which is the Insights chatbot — are sent on there.
  const navigate = useNavigate();
  const operationsLink = useRouteRef(operationsRouteRef);
  const moved = opsViewFor(searchParams.get('tab'), searchParams.get('view'));
  useEffect(() => {
    if (moved && operationsLink) navigate(`${operationsLink()}?view=${moved}`, { replace: true });
  }, [moved, operationsLink, navigate]);

  // ?tab=<area>&view=<screen>; the old ?tab=<screen> links resolve too.
  const go = useCallback(
    (areaKey: string, view?: View) => {
      const next: Record<string, string> = { tab: areaKey };
      if (view) next.view = view;
      setSearchParams(next, { replace: true });
    },
    [setSearchParams],
  );
  const openView = (view: View) => {
    const home = areaOf(areas, view);
    if (home) go(home.key, view);
  };

  const chat = useAgentChat('release');
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });
  const banner = bannerFor(chat, {
    previewOrigins: ['deploy', 'dataflow', 'releases'],
    answerOrigins: ['promote'],
    what: WHAT_OF_ORIGIN,
    doing: DOING_OF_ORIGIN,
  });
  // The screens show their form and the outcome; the decision is the banner's.
  const forScreen = (origin: string) => ({ ...chat.resultFor(origin), pendingToken: null });

  return (
    <Page themeId="tool">
      <Header title="Release Copilot" subtitle="Deploy, release and promote">
        {signedInAs && <HeaderLabel label="Signed in as" value={signedInAs} />}
      </Header>
      <Content>
        {banner && <ActionBanner {...banner} />}
        <Tabs
          className={classes.tabsBar}
          value={Math.max(0, areas.indexOf(area))}
          onChange={(_, i) => go(areas[i].key)}
          indicatorColor="primary"
        >
          {areas.map(a => (
            <Tab key={a.key} label={a.label} title={a.hint} />
          ))}
        </Tabs>
        {area.views.length > 1 && (
          <Box className={classes.views} role="tablist" aria-label={`${area.label} screens`}>
            {area.views.map(v => (
              <Chip
                key={v}
                role="tab"
                aria-selected={v === current}
                label={VIEW_LABEL[v]}
                clickable
                color={v === current ? 'primary' : 'default'}
                variant={v === current ? 'default' : 'outlined'}
                onClick={() => go(area.key, v)}
              />
            ))}
          </Box>
        )}
        <Box mt={3}>
          {current === 'deploy' && (
            <DeployTab
              onSend={sendFrom('deploy')}
              busy={chat.busy}
              result={forScreen('deploy')}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'dataflow' && (
            <DataflowTab
              onSend={sendFrom('dataflow')}
              busy={chat.busy}
              result={forScreen('dataflow')}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'releases' && (
            <ReleasesTab
              onSend={sendFrom('releases')}
              busy={chat.busy}
              result={forScreen('releases')}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
              llm={ui.llm}
            />
          )}
          {current === 'promote' && (
            <PromoteTab onSend={sendFrom('promote')} busy={chat.busy} result={forScreen('promote')} />
          )}
          {current === 'queue' && <QueueTab />}
          {current === 'history' && <HistoryTab onOpenQueue={() => openView('queue')} />}
        </Box>
      </Content>
    </Page>
  );
}
