import { createFrontendModule } from '@backstage/frontend-plugin-api';
import { ThemeBlueprint } from '@backstage/plugin-app-react';
import {
  createUnifiedTheme,
  defaultComponentThemes,
  palettes,
  UnifiedThemeProvider,
} from '@backstage/theme';
import RocketIcon from '@material-ui/icons/FlightTakeoff';
import { ReactNode } from 'react';
import { DEV_PORTAL as P } from '@internal/plugin-release-copilot';

// The standalone Dev Portal's look for the whole Backstage app: the same page
// colour and glow, glass cards, slate text, the emerald→teal accent, pill chips
// and the Inter typeface. Values come from the plugin's DEV_PORTAL tokens so
// the app and the plugin cannot drift apart. Backstage UI components (page body, plugin headers) take their colours from
// CSS variables instead — devPortal.css, scoped by themeName="dev-portal".

type Styles = Record<string, unknown>;
type Slot = Styles | ((args: any) => Styles);

// Backstage merges `components` ONE level deep: a component listed here would
// replace Backstage's own defaults for it (table rows, tab sizing, card
// padding, …) instead of adding to them. So every override is layered on top
// of the default for the same slot.
function extend(name: string, extra: Record<string, Styles>) {
  const base: any = (defaultComponentThemes as any)[name] ?? {};
  const baseSlots: Record<string, Slot> = base.styleOverrides ?? {};
  const slots: Record<string, Slot> = { ...baseSlots };
  for (const [slot, styles] of Object.entries(extra)) {
    const b = baseSlots[slot];
    slots[slot] = b
      ? (args: any) => ({ ...(typeof b === 'function' ? b(args) : b), ...styles })
      : styles;
  }
  return { ...base, styleOverrides: slots };
}

// CssBaseline's default is a function of the theme (html/body fonts, scrollbar).
function extendBaseline(extraBody: Styles) {
  const base: any = (defaultComponentThemes as any).MuiCssBaseline;
  return {
    ...base,
    styleOverrides: (muiTheme: any) => {
      const styles = base.styleOverrides(muiTheme);
      return { ...styles, body: { ...styles.body, ...extraBody } };
    },
  };
}

// A dark header with the portal's two soft glows, for every page type.
const HEADER_BG =
  'radial-gradient(900px 260px at 8% -60%, rgba(16,185,129,.42), transparent 60%), ' +
  'radial-gradient(700px 240px at 100% -20%, rgba(45,212,191,.26), transparent 55%), ' +
  'linear-gradient(135deg, #0b1324 0%, #080d1a 100%)';
const headerTheme = {
  colors: [P.emerald, P.teal],
  shape: 'none',
  backgroundImage: HEADER_BG,
  fontColor: '#ffffff',
};
const pageTheme = Object.fromEntries(
  ['home', 'tool', 'documentation', 'service', 'website', 'library', 'other', 'app', 'apis', 'card'].map(
    id => [id, headerTheme],
  ),
);

type Surfaces = {
  page: string;
  backdrop: string;
  paper: string;
  card: string;
  input: string;
  border: string;
  borderStrong: string;
  text: string;
  textMuted: string;
  textFaint: string;
};

const DARK: Surfaces = {
  page: P.page,
  backdrop: P.backdrop,
  paper: P.solid,
  card: P.glass,
  input: P.glass,
  border: P.border,
  borderStrong: P.borderStrong,
  text: P.text,
  textMuted: P.textMuted,
  textFaint: P.textFaint,
};

// Same accents on light surfaces, for anyone who prefers light.
const LIGHT: Surfaces = {
  page: '#f5f7fb',
  backdrop:
    'radial-gradient(1100px 560px at 12% -8%, rgba(16,185,129,.10), transparent 60%), ' +
    'radial-gradient(900px 520px at 100% 0%, rgba(45,212,191,.08), transparent 55%), #f5f7fb',
  paper: '#ffffff',
  card: 'rgba(255, 255, 255, 0.82)',
  input: '#ffffff',
  border: 'rgba(15, 23, 42, 0.08)',
  borderStrong: 'rgba(15, 23, 42, 0.16)',
  text: '#0f172a',
  textMuted: '#475569',
  textFaint: '#64748b',
};

function components(s: Surfaces) {
  return {
    MuiCssBaseline: extendBaseline({ background: s.backdrop, backgroundAttachment: 'fixed' }),
    MuiPaper: extend('MuiPaper', {
      root: { backgroundImage: 'none' },
    }),
    MuiCard: extend('MuiCard', {
      root: {
        borderRadius: P.radius.card,
        background: s.card,
        border: `1px solid ${s.border}`,
        boxShadow: 'none',
        backdropFilter: 'blur(16px) saturate(140%)',
      },
    }),
    MuiCardHeader: extend('MuiCardHeader', {
      title: { fontWeight: 600 },
      subheader: { color: s.textMuted },
    }),
    MuiButton: extend('MuiButton', {
      root: { textTransform: 'none', borderRadius: P.radius.control, fontWeight: 600 },
      containedPrimary: {
        background: P.gradient,
        color: P.ink,
        boxShadow: 'none',
        '&:hover': { background: P.gradient, filter: 'brightness(1.07)', boxShadow: 'none' },
      },
      outlined: { borderColor: s.borderStrong },
    }),
    MuiChip: extend('MuiChip', {
      root: { borderRadius: P.radius.pill, fontWeight: 500 },
      outlined: { borderColor: s.borderStrong, background: s.card },
      clickable: { '&:hover': { borderColor: P.emerald } },
    }),
    MuiOutlinedInput: extend('MuiOutlinedInput', {
      root: {
        borderRadius: P.radius.control,
        background: s.input,
        transition: 'box-shadow .15s, border-color .15s',
        '&.Mui-focused': { boxShadow: P.focusRing },
      },
      notchedOutline: { borderColor: s.borderStrong },
    }),
    MuiTabs: extend('MuiTabs', {
      indicator: { background: P.gradient, height: 3, borderRadius: 3 },
    }),
    MuiTab: extend('MuiTab', {
      root: { textTransform: 'none', fontWeight: 500 },
    }),
    MuiTableCell: extend('MuiTableCell', {
      root: { borderBottom: `1px solid ${s.border}` },
      head: { color: s.textFaint, fontWeight: 500 },
    }),
    MuiTooltip: extend('MuiTooltip', {
      tooltip: { background: P.solid, border: `1px solid ${P.borderStrong}`, color: P.text },
    }),
  };
}

function theme(mode: 'dark' | 'light') {
  const s = mode === 'dark' ? DARK : LIGHT;
  const base = mode === 'dark' ? palettes.dark : palettes.light;
  return createUnifiedTheme({
    fontFamily: P.font,
    palette: {
      ...base,
      primary: { main: P.emerald, light: P.emeraldLight, dark: '#059669', contrastText: P.ink },
      secondary: { main: P.teal, contrastText: P.ink },
      error: { main: P.red },
      warning: { main: '#f59e0b' },
      info: { main: P.sky },
      success: { main: P.emeraldLight },
      background: { default: s.page, paper: s.paper },
      text: { primary: s.text, secondary: s.textMuted },
      divider: s.border,
      border: s.border,
      textSubtle: s.textMuted,
      textVerySubtle: s.textFaint,
      highlight: P.mint,
      link: mode === 'dark' ? P.emeraldLight : '#059669',
      linkHover: mode === 'dark' ? P.mint : P.emerald,
      // The sidebar stays dark in both, as the portal's chrome is.
      navigation: {
        background: P.nav,
        indicator: P.emerald,
        color: P.textMuted,
        selectedColor: '#ffffff',
        navItem: { hoverBackground: 'rgba(16, 185, 129, 0.10)' },
        submenu: { background: '#0b1324' },
      },
      tabbar: { indicator: P.emerald },
    },
    defaultPageTheme: 'tool',
    pageTheme,
    components: components(s),
  });
}

const darkTheme = theme('dark');
const lightTheme = theme('light');

export const brandThemeModule = createFrontendModule({
  pluginId: 'app',
  extensions: [
    // Dark first: it is the Dev Portal's own look.
    ThemeBlueprint.make({
      name: 'brand-dark',
      params: {
        theme: {
          id: 'brand-dark',
          title: 'Dev Portal (dark)',
          variant: 'dark',
          icon: <RocketIcon />,
          Provider: ({ children }: { children: ReactNode }) => (
            <UnifiedThemeProvider theme={darkTheme} themeName="dev-portal" children={children} />
          ),
        },
      },
    }),
    ThemeBlueprint.make({
      name: 'brand-light',
      params: {
        theme: {
          id: 'brand-light',
          title: 'Dev Portal (light)',
          variant: 'light',
          icon: <RocketIcon />,
          Provider: ({ children }: { children: ReactNode }) => (
            <UnifiedThemeProvider theme={lightTheme} themeName="dev-portal" children={children} />
          ),
        },
      },
    }),
  ],
});
