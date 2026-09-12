import {
  createFrontendPlugin,
  createRouteRef,
  PageBlueprint,
} from '@backstage/frontend-plugin-api';
import ReleaseIcon from '@material-ui/icons/Publish';
import OnboardingIcon from '@material-ui/icons/School';

export const rootRouteRef = createRouteRef();
export const onboardingRouteRef = createRouteRef();

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

// Its own page (and sidebar entry), for API consumers from other teams —
// not a tab inside the release tool they have no reason to open.
const onboardingPage = PageBlueprint.make({
  name: 'onboarding',
  params: {
    routeRef: onboardingRouteRef,
    path: '/consumer-onboarding',
    title: 'Consumer onboarding',
    icon: <OnboardingIcon />,
    loader: () =>
      import('./components/OnboardingPage').then(m => <m.OnboardingPage />),
  },
});

export const releaseCopilotPlugin = createFrontendPlugin({
  pluginId: 'release-copilot',
  extensions: [releaseCopilotPage, onboardingPage],
  routes: { root: rootRouteRef, onboarding: onboardingRouteRef },
});
