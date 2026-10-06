import { useCallback, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useRouteRef } from '@backstage/frontend-plugin-api';
import { operationsRouteRef } from '../plugin';
import { Box, Button, Chip, makeStyles, Tab, Tabs, Typography } from '@material-ui/core';
import { Content, Header, HeaderLabel, Page } from '@backstage/core-components';
import { ChatGrid } from './ChatTab';
import { DeployTab } from './DeployTab';
import { DataflowTab } from './DataflowTab';
import { ReleasesTab } from './ReleasesTab';
import { QueueTab } from './QueueTab';
import { HistoryTab } from './HistoryTab';
import { useAgentChat } from './useAgentChat';
import { useSignedInAs, useUiConfig } from '../api';
import { OPS_VIEWS, VIEW_LABEL, View, areaOf, resolve, visibleAreas } from './navigation';
import { DEV_PORTAL as P } from '../look';

export type { ChatMessage } from './useAgentChat';

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
  // The screens of the chosen area: a quiet row under the area tabs.
  views: { display: 'flex', flexWrap: 'wrap', gap: theme.spacing(1), marginTop: theme.spacing(2) },
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

// Screens whose submissions show their own result (and confirm) in place.
const ORIGIN_OF_VIEW: Partial<Record<View, string>> = {
  deploy: 'deploy',
  dataflow: 'dataflow',
  releases: 'releases',
};

export function ReleaseCopilotPage() {
  const classes = useStyles();
  const ui = useUiConfig();
  const signedInAs = useSignedInAs();
  const areas = visibleAreas(ui.hiddenGroups);
  const [searchParams, setSearchParams] = useSearchParams();
  const { area, view: current } = resolve(areas, searchParams.get('tab'), searchParams.get('view'));

  // Operations moved to its own page: an old link here to one of its screens
  // (?tab=support, ?tab=monitoring …) is sent on to it.
  const navigate = useNavigate();
  const operationsLink = useRouteRef(operationsRouteRef);
  const oldOpsView = [searchParams.get('tab'), searchParams.get('view')]
    .map(x => (x ?? '').toLowerCase())
    .find(x => (OPS_VIEWS as string[]).includes(x));
  useEffect(() => {
    if (oldOpsView && operationsLink) navigate(`${operationsLink()}?view=${oldOpsView}`, { replace: true });
  }, [oldOpsView, operationsLink, navigate]);

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
  const deploy = chat.resultFor('deploy');
  const dataflow = chat.resultFor('dataflow');
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });

  // The page-level bar is for a token the current tab cannot confirm itself:
  // one raised in Chat, or one raised by a form tab you have since left.
  const showBar =
    chat.pending && !chat.busy && ORIGIN_OF_VIEW[current] !== chat.pending.origin;

  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="Deploy, release and ask the release agent"
      >
        {signedInAs && <HeaderLabel label="Signed in as" value={signedInAs} />}
      </Header>
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
          value={areas.indexOf(area)}
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
          {current === 'chat' && (
            <ChatGrid messages={chat.messages} busy={chat.busy} onSend={sendFrom('chat')} />
          )}
          {current === 'deploy' && (
            <DeployTab
              onSend={sendFrom('deploy')}
              busy={chat.busy}
              result={deploy}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'dataflow' && (
            <DataflowTab
              onSend={sendFrom('dataflow')}
              busy={chat.busy}
              result={dataflow}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
            />
          )}
          {current === 'releases' && (
            <ReleasesTab
              onSend={sendFrom('releases')}
              busy={chat.busy}
              result={chat.resultFor('releases')}
              onConfirm={chat.confirm}
              onCancel={chat.dismiss}
              llm={ui.llm}
            />
          )}
          {current === 'queue' && <QueueTab />}
          {current === 'history' && (
            <HistoryTab onOpenQueue={() => openView('queue')} />
          )}
        </Box>
      </Content>
    </Page>
  );
}
