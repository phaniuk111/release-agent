import {
  artifactLines,
  artifactNames,
  buildSummary,
  canReplace,
  chipState,
  CHIPS,
  DF_DRAFT_FIELDS,
  deployIncludeProblem,
  emptyQueueNote,
  envsOf,
  forRelease,
  itemsKey,
  localToday,
  monoReleaseNote,
  parseDeployInclude,
  PROSE_FIELDS,
  prl1Names,
  releaseDateTime,
  ReleaseFields,
  releasePayload,
  releaseProblem,
  releaseRouteText,
  withQueuedItem,
} from './releaseFormat';

const auto = (value: string, source: string) => ({ value, source });

describe('change-request fields — the same rules as the portal (core/chg.js)', () => {
  it("lists the release file's prose keys in its order, and DF drafts its summary too", () => {
    expect([...PROSE_FIELDS]).toEqual([
      'change_description',
      'change_reason',
      'associated_risk',
      'consequence',
      'user_service_impact',
    ]);
    expect([...DF_DRAFT_FIELDS]).toEqual(['change_summary', ...PROSE_FIELDS]);
  });

  it('marks a field by who wrote it last, and the person’s text as edited', () => {
    expect(chipState('Low risk.', auto('Low risk.', 'team'))).toBe('team');
    expect(chipState('  Low risk. ', auto('Low risk.', 'team'))).toBe('team');
    expect(chipState('x', auto('x', 'fallback'))).toBe('fallback');
    expect(chipState('x', auto('x', 'model-v2'))).toBe('ai');
    expect(chipState('Low risk. Reviewed.', auto('Low risk.', 'team'))).toBe(
      'edited',
    );
    expect(chipState('typed', null)).toBe('edited');
    expect(chipState('   ', auto('Low risk.', 'team'))).toBe('');
    expect(CHIPS.ai.label).toBe('AI draft');
    expect(CHIPS.team.label).toBe('Team wording');
  });

  it('lets an automatic value replace only an empty field or its own last value', () => {
    expect(canReplace('', null)).toBe(true);
    expect(canReplace('Low risk. ', auto('Low risk.', 'ai'))).toBe(true);
    expect(canReplace('My own words', auto('Low risk.', 'team'))).toBe(false);
    expect(canReplace('typed first', null)).toBe(false);
  });

  it('treats a registry URL and its name:version as one item, in any order', () => {
    expect(
      itemsKey(['https://registry.example.com/docker/orders-api:5.0.4/']),
    ).toBe('orders-api:5.0.4');
    expect(
      itemsKey(['payments-api:2', '', 'orders-api:1', 'orders-api:1']),
    ).toBe(itemsKey(['orders-api:1', 'payments-api:2']));
    expect(itemsKey(['orders-api:1'])).not.toBe(itemsKey(['orders-api:2']));
    expect(itemsKey(undefined)).toBe('');
  });

  it('names the one file and its repo in mono mode', () => {
    expect(
      monoReleaseNote('release_details.json', 'example-org/mono-repo'),
    ).toBe(
      'One file — release_details.json in example-org/mono-repo. The portal raises a PR for you to ' +
        "review and merge; you'll see the exact change before anything is pushed.",
    );
    expect(monoReleaseNote('', '')).toMatch(/CARE_RELEASE_REPO is not set/);
  });
});

describe('queued rows inside a release (core/queue.js)', () => {
  it('reads the routing the developer chose when queueing', () => {
    expect(releaseRouteText({ prl1_only: true }, false)).toBe(
      'UAT → PRL1 · PRL1-only, held back from PRD',
    );
    expect(releaseRouteText({ prl1_only: false }, false)).toBe(
      'UAT → PRL1 → PRD',
    );
    expect(releaseRouteText({ target_envs: 'prd' }, true)).toBe(
      'PRD pipeline — triggered at deploy time',
    );
    expect(releaseRouteText({ target_envs: 'prl1,prd' }, true)).toBe(
      'PRD + PRL1 pipelines — triggered at deploy time',
    );
    expect(releaseRouteText({ target_envs: '' }, true)).toBe(
      'pipelines chosen at deploy time',
    );
    expect(envsOf({ target_envs: ' prd ,, prl1' })).toEqual(['prd', 'prl1']);
  });

  it('gives each release only its own kind', () => {
    const q = [
      { artifact_name: 'orders-api', df_only: false },
      { artifact_name: 'orders-df', df_only: true },
    ];
    expect(forRelease(q, true).map(x => x.artifact_name)).toEqual([
      'orders-df',
    ]);
    expect(forRelease(q, false).map(x => x.artifact_name)).toEqual([
      'orders-api',
    ]);
    expect(forRelease(undefined, false)).toEqual([]);
  });

  it('badges a build only when it was checked', () => {
    expect(buildSummary({ build_verified: true }).state).toBe('verified');
    expect(buildSummary({ build_verified: false }).state).toBe('unverified');
    expect(buildSummary({}).state).toBe('unknown');
    expect(buildSummary(undefined).label).toBe('not checked');
  });

  it('says why the checklist is empty, pointing at the other release', () => {
    expect(emptyQueueNote(false, 2)).toBe(
      'Nothing is queued for the CARE release yet — 2 items are queued for the Dataflow release. ' +
        'Developers queue charts with Add to next release, ticking CARE. You can still type artifacts below.',
    );
    expect(emptyQueueNote(true, 0)).toMatch(
      /^Nothing is queued for the Dataflow release yet\. /,
    );
  });
});

describe('the release form’s artifact lines and payload (forms/release_form.js)', () => {
  it('reads names from name:version and registry URLs', () => {
    const text =
      'orders-api:1.2.0\n\n https://registry.example.com/docker/payments-api:3.0.1/ \nnot-an-artifact';
    expect(artifactLines(text)).toEqual([
      'orders-api:1.2.0',
      'https://registry.example.com/docker/payments-api:3.0.1/',
      'not-an-artifact',
    ]);
    expect(artifactNames(text)).toEqual(['orders-api', 'payments-api']);
  });

  it('ticks a queued item in (replacing its other version) and unticks it out', () => {
    const q = { artifact_name: 'orders-api', artifact_version: '1.2.0' };
    expect(
      withQueuedItem('orders-api:1.1.0\npayments-api:3.0.1', q, true),
    ).toBe('payments-api:3.0.1\norders-api:1.2.0');
    expect(
      withQueuedItem('payments-api:3.0.1\norders-api:1.2.0', q, false),
    ).toBe('payments-api:3.0.1');
  });

  it('takes PRL1-only from the queue, else from the manual box; DF has none', () => {
    const queued = {
      'orders-api': { prl1_only: true },
      'payments-api': { prl1_only: false },
    };
    const text = 'orders-api:1\npayments-api:2\nledger-api:3';
    expect(
      prl1Names(text, queued, new Set(['ledger-api', 'payments-api']), false),
    ).toEqual(['orders-api', 'ledger-api']);
    expect(prl1Names(text, queued, new Set(['ledger-api']), true)).toEqual([]);
  });

  it('formats datetime-local as the release sends it', () => {
    expect(releaseDateTime('2026-10-08T18:00')).toBe('2026-10-08 18:00:00');
    expect(releaseDateTime('2026-10-08T18:00:30')).toBe('2026-10-08 18:00:30');
    expect(releaseDateTime('')).toBe('');
    expect(localToday(new Date(2026, 0, 5))).toBe('2026-01-05');
  });

  const fields: ReleaseFields = {
    name: ' October 8th 2026 : Release 32 ',
    jira: 'ABC-123',
    start: '2026-10-08T18:00',
    end: '2026-10-08T20:00',
    initiator: 'dev@example.com',
    summary: 'Release 32',
    description: 'orders-api fixes the totals.',
    reason: 'Bug fix.',
    risk: 'Low risk.',
    consequence: 'Totals stay wrong.',
    impact: 'No user impact is expected.',
    repo: 'example-org/deployment-repo',
    artifacts: 'orders-api:1.2.0\npayments-api:3.0.1',
  };

  it('stops a submit in the portal’s order, with its sentences', () => {
    expect(releaseProblem(fields, '')).toBe('');
    expect(releaseProblem({ ...fields, summary: ' ' }, '')).toBe(
      'Release name, start, end, initiator and summary are required.',
    );
    expect(releaseProblem(fields, 'JIRA is required')).toBe('JIRA is required');
    expect(releaseProblem({ ...fields, repo: 'no-slash' }, '')).toBe(
      'Deployment repo is required (owner/repo).',
    );
    expect(releaseProblem({ ...fields, artifacts: '\n' }, '')).toBe(
      'At least one artifact is required.',
    );
    expect(releaseProblem({ ...fields, end: fields.start }, '')).toBe(
      'End must be after start.',
    );
  });

  it('builds the exact payload, key for key', () => {
    expect(
      releasePayload(fields, {
        isDf: false,
        jira: 'ABC-123',
        prl1Only: ['payments-api'],
      }),
    ).toEqual({
      deployment_repo: 'example-org/deployment-repo',
      release_name: 'October 8th 2026 : Release 32',
      start_date: '2026-10-08 18:00:00',
      end_date: '2026-10-08 20:00:00',
      change_initiator: 'dev@example.com',
      jira: 'ABC-123',
      change_summary: 'Release 32',
      change_description: 'orders-api fixes the totals.',
      change_reason: 'Bug fix.',
      associated_risk: 'Low risk.',
      consequence: 'Totals stay wrong.',
      user_service_impact: 'No user impact is expected.',
      prl1_only: ['payments-api'],
      df_images: [],
      artefact: ['orders-api:1.2.0', 'payments-api:3.0.1'],
      release_kind: 'care',
    });
    const df = releasePayload(
      { ...fields, artifacts: 'orders-df:1.4.2\norders-df:1.4.2' },
      { isDf: true, jira: 'ABC-123', prl1Only: [] },
    );
    expect(df.df_images).toEqual(['orders-df']);
    expect(df.release_kind).toBe('df');
    expect(Object.keys(df)).toEqual([
      'deployment_repo',
      'release_name',
      'start_date',
      'end_date',
      'change_initiator',
      'jira',
      'change_summary',
      'change_description',
      'change_reason',
      'associated_risk',
      'consequence',
      'user_service_impact',
      'prl1_only',
      'df_images',
      'artefact',
      'release_kind',
    ]);
  });
});

describe('the deploy editor (forms/parse.js parseDeployInclude)', () => {
  it('reads {"include": [...]}, a bare list or one entry', () => {
    const e = { helm_chart_name: 'orders-api', helm_chart_version: '1.2.0' };
    expect(parseDeployInclude(JSON.stringify({ include: [e] }))).toEqual({
      include: [e],
      recovered: false,
    });
    expect(parseDeployInclude(JSON.stringify([e]))).toEqual({
      include: [e],
      recovered: false,
    });
    expect(parseDeployInclude(JSON.stringify(e))).toEqual({
      include: [e],
      recovered: false,
    });
  });

  it('recovers entries pasted without commas or a wrapper', () => {
    const text =
      '{"helm_chart_name": "orders-api", "helm_chart_version": "1.2.0"}\n' +
      '{"helm_chart_name": "payments-api", "helm_chart_version": "3.0.1", "values": {"a": "}"}}';
    const parsed = parseDeployInclude(text);
    expect(parsed?.recovered).toBe(true);
    expect(
      parsed?.include.map(
        x => (x as { helm_chart_name: string }).helm_chart_name,
      ),
    ).toEqual(['orders-api', 'payments-api']);
    expect(parseDeployInclude('nothing here')).toBeNull();
  });

  it('refuses entries without a name and a version, in the portal’s words', () => {
    expect(deployIncludeProblem(null)).toBe(
      'Could not find any chart entries — each needs helm_chart_name + helm_chart_version.',
    );
    expect(deployIncludeProblem({ include: [] })).toMatch(
      /Could not find any chart entries/,
    );
    expect(
      deployIncludeProblem({
        include: [{ helm_chart_name: 'orders-api', helm_chart_version: '' }],
      }),
    ).toBe(
      'Each entry needs a non-empty helm_chart_name + helm_chart_version.',
    );
    expect(deployIncludeProblem({ include: [null] })).toMatch(
      /Each entry needs/,
    );
    expect(
      deployIncludeProblem({
        include: [
          { helm_chart_name: 'orders-api', helm_chart_version: '1.2.0' },
        ],
      }),
    ).toBe('');
  });
});
