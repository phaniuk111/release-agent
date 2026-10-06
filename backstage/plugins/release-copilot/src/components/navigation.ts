/**
 * Two pages instead of nine tabs. Release work and operating what runs are
 * different jobs, so they are different sidebar entries:
 *
 *   Release Copilot   Ship     Deploy to CARE UAT, Deploy to DF UAT, a CARE/DF release,
 *                              promote a release
 *                     Queue    the next-release queue and the release history
 *   Operations        support triage, monitoring (BQ cost + PromQL), insights (with the
 *                     release-history chat)
 *
 * A page shows its areas as tabs and an area's screens as a switcher inside it.
 * Links stay stable: ?tab=<area>&view=<screen>; the old ?tab=<screen> lands on
 * its screen, and an old Release Copilot link to an operations screen — or to the
 * chat, which now lives in Insights — is sent on to the Operations page.
 */

export const VIEWS = [
  'deploy',
  'dataflow',
  'releases',
  'promote',
  'queue',
  'history',
  'support',
  'monitoring',
  'insights',
] as const;
export type View = (typeof VIEWS)[number];

export type Area = { key: string; label: string; hint: string; views: View[] };

export const AREAS: Area[] = [
  {
    key: 'ship',
    label: 'Ship',
    hint: 'Deploy to UAT, cut a release, promote it',
    views: ['deploy', 'dataflow', 'releases', 'promote'],
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

/** Old Release Copilot links that now belong to Operations: its screens, and the
 *  chat (?tab=ask / ?tab=chat), which lives in Insights. → the Operations view. */
export function opsViewFor(tab: string | null, view: string | null): View | null {
  for (const x of [tab, view].map(v => (v ?? '').toLowerCase())) {
    if ((OPS_VIEWS as string[]).includes(x)) return x as View;
    if (x === 'ask' || x === 'chat') return 'insights';
  }
  return null;
}

export const VIEW_LABEL: Record<View, string> = {
  deploy: 'Deploy to CARE UAT',
  dataflow: 'Deploy to DF UAT',
  releases: 'CARE / DF release',
  promote: 'Promote a release',
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
  // An area named by its key wins: "queue" is both the Queue area and its
  // first screen, and ?tab=queue&view=history must open the history.
  const byKey = areas.find(a => a.key === t);
  // The old links named the screen itself: ?tab=support.
  const legacy = byKey ? undefined : areas.find(a => (a.views as string[]).includes(t));
  const area = byKey ?? legacy ?? areas[0];
  const wanted = (legacy ? t : v) as View;
  return { area, view: area.views.includes(wanted) ? wanted : area.views[0] };
}

/** The area a screen lives in (to route "open the queue" and the like). */
export function areaOf(areas: Area[], view: View): Area | undefined {
  return areas.find(a => a.views.includes(view));
}
