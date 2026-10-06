import { useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Box, Chip, makeStyles } from '@material-ui/core';
import { Content, Header, HeaderLabel, Page } from '@backstage/core-components';
import { SupportTab } from './SupportTab';
import { MonitoringTab } from './MonitoringTab';
import { InsightsTab } from './InsightsTab';
import { useAgentChat } from './useAgentChat';
import { useSignedInAs, useUiConfig } from '../api';
import { OPS_AREAS, VIEW_LABEL, View, resolve, visibleAreas } from './navigation';

const useStyles = makeStyles(theme => ({
  views: { display: 'flex', flexWrap: 'wrap', gap: theme.spacing(1), marginBottom: theme.spacing(3) },
}));

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
  const chat = useAgentChat('operations');
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
        {current === 'insights' && <InsightsTab />}
      </Content>
    </Page>
  );
}
