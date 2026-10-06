import { useCallback, useEffect, useRef, useState } from 'react';
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
import { ActionBanner } from './ActionBanner';
import { AgentMarkdown } from './AgentMarkdown';
import { useSignedInAs, useUiConfig } from '../api';
import { OPS_VIEWS, VIEW_LABEL, View, areaOf, resolve, visibleAreas } from './navigation';
import { DEV_PORTAL as P } from '../look';

export type { ChatMessage } from './useAgentChat';

const useStyles = makeStyles(theme => ({
  tabsBar: { borderBottom: `1px solid ${theme.palette.divider}` },
  // The screens of the chosen area: a quiet row under the area tabs.
  views: { display: 'flex', flexWrap: 'wrap', gap: theme.spacing(1), marginTop: theme.spacing(2) },
  pointer: {
    display: 'flex',
    alignItems: 'center',
    gap: theme.spacing(1.5),
    marginTop: theme.spacing(2),
    padding: theme.spacing(1, 2),
    borderRadius: P.radius.card,
    border: `1px solid ${P.amberBorder}`,
  },
  spacer: { flex: 1 },
  done: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: theme.spacing(1.5),
    marginBottom: theme.spacing(2),
    padding: theme.spacing(1.5, 2),
    borderRadius: P.radius.card,
    border: '1px solid rgba(52, 211, 153, 0.45)',
    background: theme.palette.type === 'light' ? 'rgba(16, 185, 129, 0.08)' : 'rgba(6, 40, 30, 0.85)',
  },
}));

// What the banner says while a confirmed decision is carried out.
const DOING_OF_ORIGIN: Record<string, string> = {
  deploy: 'Deploying to CARE UAT — merging into SIT…',
  dataflow: 'Dispatching the Dataflow deploy…',
  releases: 'Creating the release…',
  chat: 'Carrying it out…',
};

// What the banner calls a decision, by the screen (or the chat) that raised it.
const WHAT_OF_ORIGIN: Record<string, string> = {
  deploy: 'Deploy to CARE UAT',
  dataflow: 'Deploy to DF UAT',
  releases: 'CARE / DF release',
  chat: 'From the chat',
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
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });

  // The page-level bar is for a token the current tab cannot confirm itself:
  // one raised in Chat, or one raised by a form tab you have since left.
  // Previews and approvals are answered where the work is — Ship and Queue —
  // never in Ask. A screen shows its own; one raised elsewhere (typed in Ask,
  // or from a screen you have since left) waits on every Ship / Queue screen.
  const actionArea = area.key !== 'ask';
  // One banner, at the top of Ship and Queue, for every decision: a preview
  // being built, a CONFIRM token, a yes/no approval — wherever it was raised.
  const building =
    (['deploy', 'dataflow', 'releases'] as const).find(o => chat.resultFor(o).streaming) ??
    // an answer to something the chat raised (a promotion) runs as a chat turn
    (chat.answering && chat.resultFor('chat').streaming ? 'chat' : undefined);
  // What the last approval was about, so the banner can say "Promoting…" while it runs.
  const lastApproval = useRef('');
  if (chat.approval) lastApproval.current = chat.approval.message;
  const waitingOrigin = chat.approval?.origin ?? chat.pending?.origin ?? building ?? null;
  const banner =
    actionArea && waitingOrigin
      ? {
          what: chat.approval
            ? chat.approval.message.includes('Promote')
              ? 'Promotion'
              : 'Approval'
            : WHAT_OF_ORIGIN[waitingOrigin] ?? 'Preview',
          token: chat.approval ? null : chat.pending?.token ?? null,
          detail: chat.approval ? chat.approval.message : chat.resultFor(waitingOrigin).text,
          building: !chat.approval && !chat.pending && !!building && !chat.answering,
          applying:
            !chat.approval && !chat.pending && !!building && chat.answering
              ? chat.answering === 'stop'
                ? 'Cancelling — nothing will change…'
                : waitingOrigin === 'chat' && lastApproval.current.includes('Promote')
                  ? 'Promoting — copying the release files and merging…'
                  : DOING_OF_ORIGIN[waitingOrigin] ?? 'Carrying it out…'
              : null,
          steps: chat.resultFor(waitingOrigin).progress,
          onConfirm: chat.approval ? chat.approve : chat.confirm,
          onCancel: chat.approval ? chat.reject : chat.dismiss,
        }
      : null;
  // The outcome of something the CHAT raised (a promotion) has no screen of its
  // own: when its answer finishes, say how it went here, where it was answered.
  const [done, setDone] = useState<{ what: string; text: string } | null>(null);
  const answeringChat = useRef<string | null>(null);
  useEffect(() => {
    if (chat.answering === 'go' && building === 'chat') {
      answeringChat.current = banner?.what === 'Promotion' || lastApproval.current.includes('Promote') ? 'Promotion' : 'Done';
    } else if (!chat.busy && answeringChat.current) {
      setDone({ what: answeringChat.current, text: chat.resultFor('chat').text });
      answeringChat.current = null;
    }
  }, [chat.answering, chat.busy]); // eslint-disable-line react-hooks/exhaustive-deps

  // The screens show their form and the outcome; the decision is the banner's.
  const forScreen = (origin: string) => ({ ...chat.resultFor(origin), pendingToken: null });

  return (
    <Page themeId="tool">
      <Header
        title="Release Copilot"
        subtitle="Deploy, release and ask the release agent"
      >
        {signedInAs && <HeaderLabel label="Signed in as" value={signedInAs} />}
      </Header>
      <Content>
        {banner && <ActionBanner {...banner} />}
        {!banner && done && actionArea && (
          <Box className={classes.done} data-testid="done-note">
            <Box flex={1}>
              <Typography variant="subtitle2">✓ {done.what} — done</Typography>
              <AgentMarkdown text={done.text} />
            </Box>
            <Button size="small" onClick={() => setDone(null)}>
              Dismiss
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
        {!actionArea && (chat.pending || chat.approval) && !chat.busy && (
          <Box className={classes.pointer} data-testid="waiting-pointer">
            <Typography variant="body2">
              {chat.approval ? 'An approval is waiting for you.' : 'A preview is waiting for your confirmation.'}
            </Typography>
            <span className={classes.spacer} />
            <Button size="small" variant="outlined" onClick={() => go('ship')}>
              Open Ship
            </Button>
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
          {current === 'queue' && <QueueTab />}
          {current === 'history' && (
            <HistoryTab onOpenQueue={() => openView('queue')} />
          )}
        </Box>
      </Content>
    </Page>
  );
}
