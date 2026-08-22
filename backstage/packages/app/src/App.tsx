import { createApp } from '@backstage/frontend-defaults';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import { navModule } from './modules/nav';
import { homeModule } from './modules/home';
import releaseCopilotPlugin from '@internal/plugin-release-copilot';

export default createApp({
  features: [catalogPlugin, navModule, homeModule, releaseCopilotPlugin],
});
