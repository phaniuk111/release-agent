import {
  createFrontendPlugin,
  createRouteRef,
  PageBlueprint,
} from '@backstage/frontend-plugin-api';
import ReleaseIcon from '@material-ui/icons/Publish';

export const rootRouteRef = createRouteRef();

const releaseCopilotPage = PageBlueprint.make({
  params: {
    routeRef: rootRouteRef,
    path: '/release-copilot',
    title: 'Release Copilot',
    icon: <ReleaseIcon />,
    loader: () =>
      import('./components/ReleaseCopilotPage').then(m => (
        <m.ReleaseCopilotPage />
      )),
  },
});

export const releaseCopilotPlugin = createFrontendPlugin({
  pluginId: 'release-copilot',
  extensions: [releaseCopilotPage],
  routes: { root: rootRouteRef },
});
