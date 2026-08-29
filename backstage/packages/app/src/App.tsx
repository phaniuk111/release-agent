import { createApp } from '@backstage/frontend-defaults';
import catalogPlugin from '@backstage/plugin-catalog/alpha';
import { navModule } from './modules/nav';
import { homeModule } from './modules/home';
import githubActionsModule from './modules/githubActions';
import searchHotkeyModule from './modules/searchHotkey';
import entityCardsModule from './modules/entityCards';
import { brandThemeModule } from './modules/theme';
import releaseCopilotPlugin from '@internal/plugin-release-copilot';

export default createApp({
  features: [
    catalogPlugin,
    navModule,
    homeModule,
    releaseCopilotPlugin,
    githubActionsModule,
    searchHotkeyModule,
    entityCardsModule,
    brandThemeModule,
  ],
});
