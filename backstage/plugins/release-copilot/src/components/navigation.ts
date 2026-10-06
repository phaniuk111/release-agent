/**
 * Two pages instead of nine tabs. Release work and operating what runs are
 * different jobs, so they are different sidebar entries:
 *
 *   Release Copilot   Ask      the chat
 *                     Ship     Deploy to CARE UAT, Deploy to DF UAT, a CARE/DF release
 *                     Queue    the next-release queue and the release history
 *   Operations        support triage, monitoring (BQ cost + PromQL), insights
 *
 * A page shows its areas as tabs and an area's screens as a switcher inside it.
 * Links stay stable: ?tab=<area>&view=<screen>; the old ?tab=<screen> lands on
 * its screen, and an old Release Copilot link to an operations screen is sent on
 * to the Operations page (OPS_VIEWS).
 */

export const VIEWS = [
  'chat',
  'deploy',
  'dataflow',
  'releases',
  'queue',
  'history',
  'support',
  'monitoring',
  'insights',
] as const;
export type View = (typeof VIEWS)[number];

export type Area = { key: string; label: string; hint: string; views: View[] };

export const AREAS: Area[] = [
  { key: 'ask', label: 'Ask', hint: 'Ask the agent anything about releases', views: ['chat'] },
  {
    key: 'ship',
    label: 'Ship',
    hint: 'Deploy to UAT, or cut a release',
    views: ['deploy', 'dataflow', 'releases'],
  },
  { key: 'queue', label: 'Queue', hint: 'What goes in the next release', views: ['queue', 'history'] },
];

/** The Operations page: one area, its screens in the switcher. */
export const OPS_AREAS: Area[] = [
  {
    key: 'operate',
    label: 'Operations',
    hint: 'What failed, what costs, what is deployed',
    views: ['support', 'monitoring', 'insights'],
  },
];
export const OPS_VIEWS: View[] = OPS_AREAS[0].views;

export const VIEW_LABEL: Record<View, string> = {
  chat: 'Chat',
  deploy: 'Deploy to CARE UAT',
  dataflow: 'Deploy to DF UAT',
  releases: 'CARE / DF release',
  queue: 'Release queue',
  history: 'Release history',
  support: 'Support triage',
  monitoring: 'Monitoring',
  insights: 'Insights',
};

// The portal's pill group a screen belongs to, when that group can be preview
// (PREVIEW_GROUPS): a caller outside the preview does not see the screen, and
// the agent refuses its API anyway.
const GROUP_OF_VIEW: Partial<Record<View, string>> = { monitoring: 'Monitoring', support: 'Support' };

/** The areas this caller sees, each with only the screens they may use. */
export function visibleAreas(hiddenGroups: string[], areas: Area[] = AREAS): Area[] {
  return areas.map(a => ({
    ...a,
    views: a.views.filter(v => {
      const group = GROUP_OF_VIEW[v];
      return !group || !hiddenGroups.includes(group);
    }),
  })).filter(a => a.views.length > 0);
}

/** ?tab= / ?view= → the area and screen to show (first area when unknown). */
export function resolve(
  areas: Area[],
  tab: string | null,
  view: string | null,
): { area: Area; view: View } {
  const t = (tab ?? '').toLowerCase();
  const v = (view ?? '').toLowerCase();
  // The old links named the screen itself: ?tab=support.
  const legacy = areas.find(a => (a.views as string[]).includes(t));
  const area = areas.find(a => a.key === t) ?? legacy ?? areas[0];
  const wanted = (legacy ? t : v) as View;
  return { area, view: area.views.includes(wanted) ? wanted : area.views[0] };
}

/** The area a screen lives in (to route "open the queue" and the like). */
export function areaOf(areas: Area[], view: View): Area | undefined {
  return areas.find(a => a.views.includes(view));
}
