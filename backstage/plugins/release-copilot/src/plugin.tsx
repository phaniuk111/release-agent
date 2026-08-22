import {
  createFrontendPlugin,
  PageBlueprint,
} from '@backstage/frontend-plugin-api';
import ReleaseIcon from '@material-ui/icons/Publish';

const releaseCopilotPage = PageBlueprint.make({
  params: {
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
});
