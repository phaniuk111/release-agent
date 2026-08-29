import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { HomePageWidgetBlueprint } from '@backstage/plugin-home-react/alpha';
import { MarkdownContent } from '@backstage/core-components';
import {
  StatusCard,
  apiGet,
  useApiBase,
} from '@internal/plugin-release-copilot';
import { useEffect, useState } from 'react';
import { Button, Chip, makeStyles } from '@material-ui/core';
import { Link } from 'react-router-dom';

const content = `
## Release Copilot Portal 🤖

Your one portal for **releases, deploys and dataflow jobs**.

### Start here

- **Chat with the agent** — [Release Copilot](/release-copilot) answers
  "what is the current release status?" and runs deploys for you
- **Browse services** — the [Catalog](/catalog) lists services, APIs and
  deploy repos with CI/CD status
- **Scaffold** — use [Create](/create) to start something new

### Queue & Insights

- Pending deploys live in the **Queue** tab of Release Copilot
- Release history from the event log is under **Insights**
`;

const welcomeWidget = HomePageWidgetBlueprint.make({
  name: 'welcome',
  params: {
    name: 'Welcome',
    title: 'Welcome',
    description: 'Welcome card for the Release Copilot portal',
    components: async () => ({
      Content: () => <MarkdownContent content={content} />,
    }),
  },
});

// Reuses the release-copilot plugin's StatusCard — fetches /api/release-status
// through the Backstage proxy.
const releaseCopilotWidget = HomePageWidgetBlueprint.make({
  name: 'release-copilot-status',
  params: {
    name: 'ReleaseCopilotStatus',
    title: 'Release Status (Release Copilot)',
    description: 'Current release status from the Release Copilot agent',
    components: async () => ({
      Content: StatusCard,
    }),
  },
});

// Compact quick-actions card: deep-links into Release Copilot tabs plus a
// live pending-queue badge so devs see queued deploys without leaving Home.
const useQuickActionsStyles = makeStyles(theme => ({
  root: { display: 'flex', flexDirection: 'column', gap: theme.spacing(1) },
  row: { display: 'flex', alignItems: 'center', gap: theme.spacing(1) },
  spacer: { flex: 1 },
}));

function QuickActionsContent() {
  const classes = useQuickActionsStyles();
  const apiBase = useApiBase();
  const [pending, setPending] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    apiGet<{ queue?: unknown[] }>(apiBase, '/api/queue')
      .then(ctx => {
        if (alive) setPending(ctx.queue?.length ?? 0);
      })
      .catch(() => alive && setPending(null)); // agent offline — hide badge
    return () => {
      alive = false;
    };
  }, [apiBase]);

  return (
    <div className={classes.root}>
      <div className={classes.row}>
        <Button
          component={Link}
          to="/release-copilot?tab=deploy"
          variant="contained"
          color="primary"
          size="small"
        >
          Deploy
        </Button>
        <Button
          component={Link}
          to="/release-copilot?tab=dataflow"
          variant="outlined"
          size="small"
        >
          Dataflow
        </Button>
        <Button
          component={Link}
          to="/release-copilot?tab=releases"
          variant="outlined"
          size="small"
        >
          Releases
        </Button>
      </div>
      <div className={classes.row}>
        <Button
          component={Link}
          to="/release-copilot?tab=queue"
          variant="outlined"
          size="small"
        >
          Release Queue
        </Button>
        {pending !== null && (
          <Chip
            size="small"
            label={`${pending} pending`}
            color={pending > 0 ? 'secondary' : 'default'}
          />
        )}
        <span className={classes.spacer} />
        <Button component={Link} to="/release-copilot?tab=chat" size="small">
          Ask the agent →
        </Button>
      </div>
    </div>
  );
}

const quickActionsWidget = HomePageWidgetBlueprint.make({
  name: 'quick-actions',
  params: {
    name: 'QuickActions',
    title: 'Quick Actions',
    description: 'Common release operations, one click away',
    components: async () => ({
      Content: QuickActionsContent,
    }),
  },
});

export const homeModule = createFrontendModule({
  pluginId: 'home',
  extensions: [welcomeWidget, releaseCopilotWidget, quickActionsWidget],
});
