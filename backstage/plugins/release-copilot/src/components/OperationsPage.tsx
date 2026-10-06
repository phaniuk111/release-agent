import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Box, Chip, Grid, makeStyles } from '@material-ui/core';
import { Content, Header, HeaderLabel, Page } from '@backstage/core-components';
import { SupportTab } from './SupportTab';
import { MonitoringTab } from './MonitoringTab';
import { InsightsTab } from './InsightsTab';
import { ChatTab, QuickAsk, StatusCard } from './ChatTab';
import { ActionBanner, bannerFor } from './ActionBanner';
import { useAgentChat } from './useAgentChat';
import { useSignedInAs, useUiConfig } from '../api';
import { OPS_AREAS, VIEW_LABEL, View, resolve, visibleAreas } from './navigation';

const useStyles = makeStyles(theme => ({
  views: { display: 'flex', flexWrap: 'wrap', gap: theme.spacing(1), marginBottom: theme.spacing(3) },
}));

// Insights questions are about releases already made: say so, so the agent
// answers from the release log (the release-stats tools), as the onboarding
// page does for its own questions.
const HISTORY_CONTEXT = 'Release history question: ';

const HISTORY_ASKS: QuickAsk[] = [
  {
    label: 'Last 3 releases',
    hint: 'The three most recent releases — what was in them and where they went',
    text: 'What were the last three releases, what was in each, and where did they go?',
  },
  {
    label: 'What changed in PRD',
    hint: 'Charts deployed to PRD in the last 14 days',
    text: 'Which charts changed in PRD in the last 14 days?',
  },
  {
    label: 'Who released most',
    hint: 'Who raised releases recently',
    text: 'Who raised the most releases in the last 30 days?',
  },
];

/**
 * Operations: what failed overnight, what it costs, what is deployed — its own
 * sidebar entry, not a corner of the release tool. Investigate and Ask why run
 * on this page's own conversation, and their answers land on the screen that
 * asked.
 */
export function OperationsPage() {
  const classes = useStyles();
  const ui = useUiConfig();
  const signedInAs = useSignedInAs();
  const areas = visibleAreas(ui.hiddenGroups, OPS_AREAS);
  const [searchParams, setSearchParams] = useSearchParams();
  const chat = useAgentChat('operations', { answerIn: 'the banner above' });
  // Insights questions are about releases already made; anything one of them
  // raises to decide still comes up in the banner, never silently.
  const banner = bannerFor(chat, { previewOrigins: [], answerOrigins: ['chat'], what: {}, doing: {} });
  const askHistory = (text: string) =>
    chat.send(HISTORY_CONTEXT + text, { origin: 'chat', display: text });
  const sendFrom = (origin: string) => (text: string) => chat.send(text, { origin });

  const show = useCallback(
    (view: View) => setSearchParams({ view }, { replace: true }),
    [setSearchParams],
  );

  if (areas.length === 0) {
    return (
      <Page themeId="tool">
        <Header title="Operations" subtitle="Nothing here is open to you yet." />
        <Content />
      </Page>
    );
  }
  const { area, view: current } = resolve(
    areas,
    searchParams.get('tab'),
    searchParams.get('view') ?? searchParams.get('tab'),
  );

  return (
    <Page themeId="tool">
      <Header title="Operations" subtitle={area.hint}>
        {signedInAs && <HeaderLabel label="Signed in as" value={signedInAs} />}
      </Header>
      <Content>
        {banner && <ActionBanner {...banner} />}
        {area.views.length > 1 && (
          <Box className={classes.views} role="tablist" aria-label="Operations screens">
            {area.views.map(v => (
              <Chip
                key={v}
                role="tab"
                aria-selected={v === current}
                label={VIEW_LABEL[v]}
                clickable
                color={v === current ? 'primary' : 'default'}
                variant={v === current ? 'default' : 'outlined'}
                onClick={() => show(v)}
              />
            ))}
          </Box>
        )}
        {current === 'support' && (
          <SupportTab
            onSend={sendFrom('support')}
            busy={chat.busy}
            result={chat.resultFor('support')}
            llm={ui.llm}
            investigation={chat.investigationFor('support')}
          />
        )}
        {current === 'monitoring' && (
          <MonitoringTab
            onSend={sendFrom('monitoring')}
            busy={chat.busy}
            result={chat.resultFor('monitoring')}
            llm={ui.llm}
          />
        )}
        {current === 'insights' && (
          <>
            <Grid container spacing={3}>
              <Grid item xs={12} md={7}>
                <ChatTab
                  messages={chat.messages}
                  busy={chat.busy}
                  onSend={askHistory}
                  title="Ask about release history"
                  subheader="What was released when, by whom, and what is deployed where — answered from the release log"
                  emptyHint='Try: "what went to PRD in the last two weeks?"'
                  placeholder="Ask about past releases…"
                  quickAsks={HISTORY_ASKS}
                />
              </Grid>
              <Grid item xs={12} md={5}>
                <StatusCard />
              </Grid>
            </Grid>
            <Box mt={3}>
              <InsightsTab />
            </Box>
          </>
        )}
      </Content>
    </Page>
  );
}
