import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { ThemeBlueprint } from '@backstage/plugin-app-react';
import {
  createUnifiedTheme,
  UnifiedThemeProvider,
  genPageTheme,
  palettes,
  shapes,
} from '@backstage/theme';
import RocketIcon from '@material-ui/icons/FlightTakeoff';
import { ReactNode } from 'react';

// Brand colors for the Release Copilot portal. Swap these for the real
// corporate palette when available.
const BRAND = {
  primary: '#1e5eff', // vivid blue — primary actions
  secondary: '#00b8a9', // teal accent, echos the existing logo color
  highlight: '#7c3aed', // violet for links/highlight
};

const lightTheme = createUnifiedTheme({
  palette: {
    ...palettes.light,
    primary: { main: BRAND.primary },
    secondary: { main: BRAND.secondary },
    highlight: BRAND.highlight,
    navigation: {
      background: '#111827', // slate-900 sidebar
      indicator: BRAND.secondary,
      color: '#d1d5db',
      selectedColor: '#ffffff',
    },
  },
  defaultPageTheme: 'tool',
  pageTheme: {
    tool: genPageTheme({ colors: ['#1e5eff', '#00b8a9'], shape: shapes.wave }),
    home: genPageTheme({ colors: ['#1e5eff', '#7c3aed'], shape: shapes.wave }),
  },
});

const darkTheme = createUnifiedTheme({
  palette: {
    ...palettes.dark,
    primary: { main: BRAND.primary },
    secondary: { main: BRAND.secondary },
    highlight: BRAND.highlight,
    navigation: {
      background: '#0b1220',
      indicator: BRAND.secondary,
      color: '#d1d5db',
      selectedColor: '#ffffff',
    },
  },
  defaultPageTheme: 'tool',
});

export const brandThemeModule = createFrontendModule({
  pluginId: 'app',
  extensions: [
    ThemeBlueprint.make({
      name: 'brand-light',
      params: {
        theme: {
          id: 'brand-light',
          title: 'Light',
          variant: 'light',
          icon: <RocketIcon />,
          Provider: ({ children }: { children: ReactNode }) => (
            <UnifiedThemeProvider theme={lightTheme} children={children} />
          ),
        },
      },
    }),
    ThemeBlueprint.make({
      name: 'brand-dark',
      params: {
        theme: {
          id: 'brand-dark',
          title: 'Dark',
          variant: 'dark',
          icon: <RocketIcon />,
          Provider: ({ children }: { children: ReactNode }) => (
            <UnifiedThemeProvider theme={darkTheme} children={children} />
          ),
        },
      },
    }),
  ],
});
