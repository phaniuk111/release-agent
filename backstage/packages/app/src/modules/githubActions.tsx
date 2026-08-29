import {
  createFrontendModule,
  ApiBlueprint,
} from '@backstage/frontend-plugin-api';
import { configApiRef } from '@backstage/core-plugin-api';
import { scmAuthApiRef } from '@backstage/integration-react';
import { EntityContentBlueprint } from '@backstage/plugin-catalog-react/alpha';
import { Grid } from '@material-ui/core';
import {
  EntityRecentGithubActionsRunsCard,
  EntityLatestGithubActionRunCard,
  GithubActionsClient,
  githubActionsApiRef,
} from '@backstage/plugin-github-actions';

// NFS needs an explicit implementation for the legacy plugin's apiRef; the
// legacy plugin registered its API factory itself, which NFS does not run.
const githubActionsApi = ApiBlueprint.make({
  name: 'github-actions',
  params: defineParams =>
    defineParams({
      api: githubActionsApiRef,
      deps: { configApi: configApiRef, scmAuthApi: scmAuthApiRef },
      factory: ({ configApi, scmAuthApi }) =>
        new GithubActionsClient({ configApi, scmAuthApi }),
    }),
});

/**
 * New Frontend System shim around the legacy @backstage/plugin-github-actions
 * plugin. We use the plain (non-routable) entity-aware card components —
 * EntityGithubActionsContent is a legacy routable extension whose routeRef is
 * not discovered under NFS. These cards read github.com/project-slug from the
 * entity themselves and render workflow runs under a "CI/CD" entity-content
 * tab (deployment group).
 */
function GithubActionsContent() {
  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <EntityRecentGithubActionsRunsCard limit={10} />
      </Grid>
      <Grid item xs={12}>
        <EntityLatestGithubActionRunCard branch="master" />
      </Grid>
    </Grid>
  );
}

const githubActionsEntityContent = EntityContentBlueprint.make({
  name: 'github-actions',
  params: {
    path: '/ci-cd',
    title: 'CI/CD',
    group: 'deployment',
    filter: { kind: 'component' },
    loader: () => Promise.resolve(<GithubActionsContent />),
  },
});

export default createFrontendModule({
  pluginId: 'app',
  extensions: [githubActionsEntityContent, githubActionsApi],
});
