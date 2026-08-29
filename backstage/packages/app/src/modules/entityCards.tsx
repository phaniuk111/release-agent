import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { EntityCardBlueprint } from '@backstage/plugin-catalog-react/alpha';
import { EntityLatestGithubActionRunCard } from '@backstage/plugin-github-actions';
import { apiGet, useApiBase } from '@internal/plugin-release-copilot';
import { useEffect, useState } from 'react';
import { Chip, makeStyles } from '@material-ui/core';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, Typography } from '@material-ui/core';

const GH_SLUG_ANNOTATION = 'github.com/project-slug';

// === CI/CD badge on Overview: latest GHA run for the entity, no tab click ===
const cicdBadgeCard = EntityCardBlueprint.make({
  name: 'cicd-latest-run',
  params: {
    // Only for components that declare a github.com/project-slug annotation.
    filter: entity =>
      Boolean(entity.metadata.annotations?.[GH_SLUG_ANNOTATION]),
    loader: () =>
      Promise.resolve(
        <EntityLatestGithubActionRunCard branch="master" variant="gridItem" />,
      ),
  },
});

// === Release Copilot queue card: pending deploys + link, right on Overview ===
const useStyles = makeStyles(theme => ({
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: theme.spacing(1),
  },
}));

function ReleaseQueueCard() {
  const classes = useStyles();
  const apiBase = useApiBase();
  const [pending, setPending] = useState<number | null>(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    let alive = true;
    apiGet<{ queue?: unknown[] }>(apiBase, '/api/queue')
      .then(ctx => {
        if (alive) setPending(ctx.queue?.length ?? 0);
      })
      .catch(() => alive && setOffline(true));
    return () => {
      alive = false;
    };
  }, [apiBase]);

  if (offline) return null; // agent not running — hide the card entirely

  return (
    <Card variant="outlined">
      <CardHeader title="Releases" />
      <CardContent>
        {pending === null ? (
          <Typography color="textSecondary">Loading…</Typography>
        ) : (
          <div className={classes.row}>
            <Chip
              size="small"
              label={`${pending} pending deploy${pending === 1 ? '' : 's'}`}
              color={pending > 0 ? 'secondary' : 'default'}
            />
            <Link to="/release-copilot?tab=queue">Open Release Copilot →</Link>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const releaseQueueCard = EntityCardBlueprint.make({
  name: 'release-copilot-queue',
  params: {
    filter: 'kind:component',
    loader: () => Promise.resolve(<ReleaseQueueCard />),
  },
});

export default createFrontendModule({
  pluginId: 'app',
  extensions: [cicdBadgeCard, releaseQueueCard],
});
