import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { catalogApiRef } from '@backstage/plugin-catalog-react';
import { HomePageWidgetBlueprint } from '@backstage/plugin-home-react/alpha';
import { MarkdownContent } from '@backstage/core-components';
import {
  StatusCard,
  apiGet,
  useApiBase,
} from '@internal/plugin-release-copilot';
import { useEffect, useState } from 'react';
import { useApi } from '@backstage/core-plugin-api';
import { Progress } from '@backstage/core-components';
import {
  Button,
  Chip,
  Divider,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
  makeStyles,
  Typography,
} from '@material-ui/core';
import { Link } from 'react-router-dom';
import DashboardIcon from '@material-ui/icons/Dashboard';
import { z } from 'zod';

export type DashboardLink = {
  title: string;
  url: string;
  description?: string;
};

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
    apiGet<{ queue?: unknown[] }>(apiBase, '/api/release-queue')
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

// Grafana PRD dashboards gallery - config-driven (app-config under
// home-page-widget:home/grafana-dashboards -> config.dashboards), so platform
// admins curate the list once and every dev sees it on Home.
const useGrafanaListStyles = makeStyles(theme => ({
  list: { padding: 0 },
  icon: { minWidth: 36 },
  link: { color: theme.palette.primary.main, textDecoration: 'none' },
}));

function DashboardListContent({ dashboards }: { dashboards: DashboardLink[] }) {
  const classes = useGrafanaListStyles();
  if (!dashboards.length) {
    return (
      <MarkdownContent content="*No dashboards configured - add them in app-config.yaml under home-page-widget:home/grafana-dashboards.*" />
    );
  }
  return (
    <List className={classes.list} dense>
      {dashboards.map(d => (
        <ListItem
          key={d.url}
          button
          component="a"
          href={d.url}
          target="_blank"
          rel="noopener noreferrer"
        >
          <ListItemIcon className={classes.icon}>
            <DashboardIcon />
          </ListItemIcon>
          <ListItemText
            primary={d.title}
            secondary={d.description}
            primaryTypographyProps={{ className: classes.link }}
          />
        </ListItem>
      ))}
    </List>
  );
}

// Per-service dashboards: every 'service' component in the catalog gets a
// link automatically, built from a URL template ({name} substituted) - so
// adding a service to the catalog puts its dashboard on Home with zero config.
type ServiceTemplate = {
  label: string;
  urlPattern: string;
  description?: string;
};

function ServiceDashboardsSections({
  templates,
}: {
  templates: ServiceTemplate[];
}) {
  const classes = useGrafanaListStyles();
  const catalogApi = useApi(catalogApiRef);
  const [services, setServices] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    catalogApi
      .getEntities({
        filter: { kind: 'component' },
        fields: ['metadata.name', 'spec.type'],
      })
      .then(({ items }) => {
        if (!alive) return;
        setServices(
          items
            .filter(e => (e.spec as { type?: string })?.type === 'service')
            .map(e => e.metadata.name)
            .sort(),
        );
      })
      .catch(e => alive && setError((e as Error).message));
    return () => {
      alive = false;
    };
  }, [catalogApi]);

  if (error) {
    return <MarkdownContent content={`*Catalog error: ${error}*`} />;
  }
  if (services === null) {
    return <Progress />;
  }
  if (!services.length) {
    return <MarkdownContent content="*No services in the catalog yet.*" />;
  }
  return (
    <>
      {templates.map(t => (
        <div key={t.label}>
          <Typography variant="subtitle2" style={{ marginTop: 8 }}>
            {t.label}
          </Typography>
          <List className={classes.list} dense>
            {services.map(name => {
              const url = t.urlPattern.replace(/\{\{name\}\}/g, name);
              return (
                <ListItem
                  key={`${t.label}-${name}`}
                  button
                  component="a"
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ListItemIcon className={classes.icon}>
                    <DashboardIcon />
                  </ListItemIcon>
                  <ListItemText
                    primary={name}
                    secondary={t.description}
                    primaryTypographyProps={{ className: classes.link }}
                  />
                </ListItem>
              );
            })}
          </List>
        </div>
      ))}
    </>
  );
}

function makeGrafanaTileWidget(
  name: string,
  widgetName: string,
  title: string,
  description: string,
) {
  return HomePageWidgetBlueprint.makeWithOverrides({
    name,
    configSchema: {
      dashboards: z
        .array(
          z.object({
            title: z.string(),
            url: z.string(),
            description: z.string().optional(),
          }),
        )
        .optional(),
      // One section per environment: every 'service' component in the catalog
      // gets a dashboard link built from the template ({{name}} substituted).
      serviceDashboardTemplates: z
        .array(
          z.object({
            label: z.string(),
            urlPattern: z.string(),
            description: z.string().optional(),
          }),
        )
        .optional(),
    },
    *factory(originalFactory, { config }) {
      yield* originalFactory({
        name: widgetName,
        title,
        description,
        components: async () => ({
          Content: () => {
            const templates = config.serviceDashboardTemplates ?? [];
            return (
              <>
                <DashboardListContent dashboards={config.dashboards ?? []} />
                {templates.length > 0 && (
                  <>
                    <Divider style={{ margin: '8px 0' }} />
                    <ServiceDashboardsSections templates={templates} />
                  </>
                )}
              </>
            );
          },
        }),
      });
    },
  });
}

const grafanaDashboardsWidget = makeGrafanaTileWidget(
  'grafana-dashboards',
  'GrafanaDashboards',
  'Grafana - PRD Dashboards',
  'Observability dashboards for every service',
);

const grafanaUatDashboardsWidget = makeGrafanaTileWidget(
  'grafana-uat-dashboards',
  'GrafanaUatDashboards',
  'Grafana - UAT Dashboards',
  'Pre-prod observability for every service',
);

export const homeModule = createFrontendModule({
  pluginId: 'home',
  extensions: [
    welcomeWidget,
    releaseCopilotWidget,
    quickActionsWidget,
    grafanaDashboardsWidget,
    grafanaUatDashboardsWidget,
  ],
});
