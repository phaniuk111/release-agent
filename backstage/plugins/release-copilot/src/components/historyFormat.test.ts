import {
  addLabel,
  emptyListText,
  filterReleases,
  HistoryItem,
  HistoryRelease,
  itemKind,
  itemState,
  missingInputs,
  missingWhy,
  noneWhy,
  queueDestination,
  releaseSummary,
  requeueAllIndices,
  requeuePlan,
  safeRunHref,
  ShownRelease,
  withTyped,
} from './historyFormat';

// The same cases as the portal's tests/js/history.test.mjs — both UIs must agree.

const RUN = 'https://github.com/org/build/actions/runs/123456';

// A never-queued row by default: typed straight into a release form.
const item = (over: HistoryItem = {}): HistoryItem => ({
  artifact_name: 'chart',
  artifact_version: '1.0.0',
  jira_ticket: '',
  build_run_url: '',
  prl1_only: false,
  df_only: false,
  target_envs: 'prd,prl1',
  change_details: '',
  note: '',
  queued_by: '',
  in_queue: false,
  from_queue: false,
  requeueable: false,
  ...over,
});

const RELEASES: HistoryRelease[] = [
  {
    release_name: 'CARE-42',
    pr_number: 7,
    items: [
      item({ artifact_name: 'payments-api', artifact_version: '2.3.1', jira_ticket: 'PAY-101', from_queue: true }),
      item({ artifact_name: 'ledger', artifact_version: '5.0.0', jira_ticket: 'LED-9', in_queue: true, from_queue: true }),
      item({ artifact_name: 'fx-rates', artifact_version: '0.9.0' }),
    ],
  },
  {
    release_name: 'DF-7',
    pr_number: 8,
    items: [
      item({ artifact_name: 'df-ingest', artifact_version: '3.1.0', jira_ticket: 'ING-5', df_only: true, from_queue: true }),
    ],
  },
  {
    release_name: 'CARE-41',
    pr_number: 6,
    items: [
      item({ artifact_name: 'payments-ui', artifact_version: '1.2.3', jira_ticket: 'PAY-77', build_run_url: RUN }),
    ],
  },
];

// Only the original positions: what the tick keys ("ri:ii") are built from.
const positions = (shown: ShownRelease[]) => shown.map(r => [r.index, r.items.map(x => x.index)]);

describe('itemKind / itemState', () => {
  it('a Dataflow image is DF, everything else CARE', () => {
    expect(itemKind(item({ df_only: true }))).toBe('DF');
    expect(itemKind(item({ df_only: false }))).toBe('CARE');
    expect(itemKind({})).toBe('CARE');
  });

  it('a row already in the next release is in-queue, whatever else it has', () => {
    expect(itemState(item({ in_queue: true, from_queue: true }))).toBe('in-queue');
    expect(itemState(item({ in_queue: true, build_run_url: RUN, jira_ticket: 'X-1' }))).toBe('in-queue');
  });

  it('a row that qualified once goes straight back — no run or ticket needed here', () => {
    expect(itemState(item({ from_queue: true }))).toBe('ready');
  });

  it('a never-queued row is ready only with a real run URL AND a ticket', () => {
    expect(itemState(item({ build_run_url: RUN, jira_ticket: 'PAY-1' }))).toBe('ready');
    expect(itemState(item({ build_run_url: `  ${RUN}  `, jira_ticket: 'PAY-1' }))).toBe('ready');
    expect(itemState(item({ build_run_url: RUN }))).toBe('needs-input');
    expect(itemState(item({ build_run_url: RUN, jira_ticket: '   ' }))).toBe('needs-input');
    expect(itemState(item({ jira_ticket: 'PAY-1' }))).toBe('needs-input');
    expect(itemState(item())).toBe('needs-input');
    for (const bad of [
      'https://github.com/org/build/actions/runs/',
      'https://github.com/org/build/actions/runs/latest',
      'https://github.com/org/build/actions',
      'github.com/org/build/actions/runs/123',
      'ftp://github.com/org/build/actions/runs/123',
      'see https://github.com/org/build/actions/runs/123',
    ]) {
      expect([bad, itemState(item({ build_run_url: bad, jira_ticket: 'PAY-1' }))]).toEqual([bad, 'needs-input']);
    }
    expect(itemState(item({ build_run_url: `${RUN}/job/99`, jira_ticket: 'PAY-1' }))).toBe('ready');
  });
});

describe('filterReleases', () => {
  it('with no query and every kind, every release and row is shown, in order', () => {
    const shown = filterReleases(RELEASES);
    expect(positions(shown)).toEqual([[0, [0, 1, 2]], [1, [0]], [2, [0]]]);
    expect(shown[0].rel).toBe(RELEASES[0]);
    expect(shown[0].items[2].it).toBe(RELEASES[0].items![2]);
    expect(positions(filterReleases(RELEASES, { query: '   ', kind: 'all' }))).toEqual(positions(shown));
  });

  it('the query finds a row by chart name, keeping its original positions', () => {
    expect(positions(filterReleases(RELEASES, { query: 'fx-rates' }))).toEqual([[0, [2]]]);
    expect(positions(filterReleases(RELEASES, { query: 'payments' }))).toEqual([[0, [0]], [2, [0]]]);
  });

  it('the query finds a row by version and by JIRA key', () => {
    expect(positions(filterReleases(RELEASES, { query: '3.1.0' }))).toEqual([[1, [0]]]);
    expect(positions(filterReleases(RELEASES, { query: 'LED-9' }))).toEqual([[0, [1]]]);
    expect(positions(filterReleases(RELEASES, { query: 'PAY-' }))).toEqual([[0, [0]], [2, [0]]]);
  });

  it('the query ignores case and surrounding whitespace', () => {
    expect(positions(filterReleases(RELEASES, { query: '  Led-9 ' }))).toEqual([[0, [1]]]);
    expect(positions(filterReleases(RELEASES, { query: 'PAYMENTS-UI' }))).toEqual([[2, [0]]]);
  });

  it('the query does not match the release name or anything outside the three fields', () => {
    expect(filterReleases(RELEASES, { query: 'CARE-42' })).toEqual([]);
    expect(filterReleases(RELEASES, { query: 'prd,prl1' })).toEqual([]);
  });

  it('the kind filter keeps only CARE charts or only DF images', () => {
    expect(positions(filterReleases(RELEASES, { kind: 'DF' }))).toEqual([[1, [0]]]);
    expect(positions(filterReleases(RELEASES, { kind: 'CARE' }))).toEqual([[0, [0, 1, 2]], [2, [0]]]);
    expect(positions(filterReleases(RELEASES, { kind: 'all' }))).toEqual([[0, [0, 1, 2]], [1, [0]], [2, [0]]]);
  });

  it('query and kind combine; nothing left means nothing shown', () => {
    expect(positions(filterReleases(RELEASES, { query: 'ing', kind: 'DF' }))).toEqual([[1, [0]]]);
    expect(filterReleases(RELEASES, { query: 'df-ingest', kind: 'CARE' })).toEqual([]);
    expect(filterReleases(RELEASES, { query: 'no-such-chart' })).toEqual([]);
  });

  it('a release with no rows, or no releases at all, shows nothing', () => {
    expect(filterReleases([{ release_name: 'empty', items: [] }])).toEqual([]);
    expect(filterReleases([])).toEqual([]);
    expect(filterReleases(undefined)).toEqual([]);
  });
});

describe('requeueAllIndices', () => {
  it('"Re-queue all" ticks only the ready rows, never in-queue or needs-input', () => {
    expect(requeueAllIndices(RELEASES[0])).toEqual([0]);
    expect(requeueAllIndices(RELEASES[2])).toEqual([0]);
    expect(
      requeueAllIndices({
        items: [
          item({ build_run_url: RUN }),
          item({ from_queue: true }),
          item({ jira_ticket: 'X-1' }),
          item({ build_run_url: RUN, jira_ticket: 'X-1' }),
          item({ in_queue: true, from_queue: true }),
        ],
      }),
    ).toEqual([1, 3]);
    expect(requeueAllIndices({ items: [] })).toEqual([]);
    expect(requeueAllIndices(undefined)).toEqual([]);
  });

  it('under a filter ticks only the rows the filter shows', () => {
    const rel = {
      items: [
        { artifact_name: 'a', from_queue: true },
        { artifact_name: 'b', from_queue: true },
        { artifact_name: 'c', in_queue: true },
      ],
    };
    expect(requeueAllIndices(rel)).toEqual([0, 1]);
    expect(requeueAllIndices(rel, [1, 2])).toEqual([1]);
    expect(requeueAllIndices(rel, [])).toEqual([]);
  });
});

describe('releaseSummary', () => {
  it('counts each state and leaves out the empty ones', () => {
    expect(
      releaseSummary({ items: [item({ from_queue: true }), item({ from_queue: true }), item({ in_queue: true })] }),
    ).toBe('3 charts · 2 ready · 1 in next release');
    expect(releaseSummary(RELEASES[0])).toBe('3 charts · 1 ready · 1 in next release · 1 needs run + ticket');
    expect(
      releaseSummary({ items: [item({ from_queue: true }), item({ build_run_url: RUN }), item({ build_run_url: RUN })] }),
    ).toBe('3 charts · 1 ready · 2 need ticket');
    expect(releaseSummary({ items: [item({ from_queue: true }), item({ jira_ticket: 'X-1' })] })).toBe(
      '2 charts · 1 ready · 1 needs run',
    );
  });

  it('when one state covers every chart, the summary does not repeat the count', () => {
    expect(releaseSummary({ items: [item()] })).toBe('1 chart · needs run + ticket');
    expect(releaseSummary({ items: [item({ from_queue: true })] })).toBe('1 chart · ready');
    expect(releaseSummary({ items: [item({ in_queue: true })] })).toBe('1 chart · in next release');
    expect(releaseSummary({ items: [item({ from_queue: true }), item({ from_queue: true })] })).toBe(
      '2 charts · all ready',
    );
    expect(releaseSummary({ items: [item({ jira_ticket: 'X-1' }), item({ build_run_url: RUN })] })).toBe(
      '2 charts · all need run + ticket',
    );
  });

  it('a release with no charts says so', () => {
    expect(releaseSummary({ items: [] })).toBe('0 charts');
    expect(releaseSummary(undefined)).toBe('0 charts');
  });
});

describe('missingInputs / withTyped / safeRunHref', () => {
  it('missingInputs names what the gate still needs, only for a never-queued row', () => {
    const run = 'https://github.com/o/r/actions/runs/123';
    expect(missingInputs({})).toEqual(['run', 'ticket']);
    expect(missingInputs({ build_run_url: run })).toEqual(['ticket']);
    expect(missingInputs({ jira_ticket: 'ABC-1' })).toEqual(['run']);
    expect(missingInputs({ build_run_url: 'https://x/not-a-run', jira_ticket: 'ABC-1' })).toEqual(['run']);
    expect(missingInputs({ build_run_url: run, jira_ticket: 'ABC-1' })).toEqual([]);
    expect(missingInputs({ from_queue: true })).toEqual([]);
    expect(missingInputs({ in_queue: true })).toEqual([]);
  });

  it('withTyped lays typed values over the record: trimmed, the ticket upper-cased, untyped fields kept', () => {
    const rec = { artifact_name: 'a', build_run_url: 'old', jira_ticket: 'OLD-1' };
    expect(withTyped(rec, { run: '  https://x/actions/runs/9 ', jira: ' abc-2 ' })).toEqual({
      artifact_name: 'a',
      build_run_url: 'https://x/actions/runs/9',
      jira_ticket: 'ABC-2',
    });
    expect(withTyped(rec, { jira: 'x-1' })).toEqual({ artifact_name: 'a', build_run_url: 'old', jira_ticket: 'X-1' });
    expect(withTyped(rec, undefined)).toEqual(rec);
    expect(withTyped(rec, {})).not.toBe(rec);
  });

  it('only an http(s) run URL becomes a link', () => {
    expect(safeRunHref(' https://github.com/o/r/actions/runs/1 ')).toBe('https://github.com/o/r/actions/runs/1');
    expect(safeRunHref('javascript:alert(1)')).toBe('');
    expect(safeRunHref('JaVaScRiPt:alert(1)')).toBe('');
    expect(safeRunHref('data:text/html,x')).toBe('');
    expect(safeRunHref('')).toBe('');
  });
});

describe('requeuePlan and queueDestination (from core/queue.js)', () => {
  it('splits ticked rows: qualified once → direct, never queued with run + ticket → gated, else skipped', () => {
    const plan = requeuePlan([
      item({ artifact_name: 'orders-api', artifact_version: '1.0.0', from_queue: true }),
      item({ artifact_name: 'payments-api', artifact_version: '2.0.0', build_run_url: RUN, jira_ticket: 'ABC-123' }),
      item({ artifact_name: 'ledger', in_queue: true }),
      item({ artifact_name: 'fx-rates' }),
      item({ artifact_name: 'df-ingest', build_run_url: RUN }),
    ]);
    expect(plan.direct).toEqual([{ artifact_name: 'orders-api', artifact_version: '1.0.0' }]);
    expect(plan.gated).toEqual([
      {
        artifact: 'payments-api:2.0.0',
        build_run_url: RUN,
        jira_ticket: 'ABC-123',
        prl1_only: false,
        df_only: false,
        target_envs: 'prd,prl1',
        change_details: '',
        note: '',
      },
    ]);
    expect(plan.skipped).toEqual([
      { artifact: 'ledger:1.0.0', reason: 'already queued for the next release' },
      {
        artifact: 'fx-rates:1.0.0',
        reason: 'never went through the queue — paste the run that built it in its row, then tick',
      },
      {
        artifact: 'df-ingest:1.0.0',
        reason: 'never went through the queue — the gate needs a JIRA ticket; add it in its row, then tick',
      },
    ]);
  });

  it('reads the routing the same way as the queue tab', () => {
    expect(queueDestination({})).toBe('CARE → UAT, PRL1, PRD');
    expect(queueDestination({ prl1_only: true })).toBe('CARE → UAT, PRL1');
    expect(queueDestination({ df_only: true, target_envs: 'prl1,prd' })).toBe('DF → PRD, PRL1');
    expect(queueDestination({ df_only: true })).toBe('DF (Dataflow)');
  });
});

describe('the screen sentences', () => {
  it('say what a row or release still lacks', () => {
    expect(missingWhy({})).toBe(
      'Never went through the queue — paste the run that built it and its JIRA ticket, then tick',
    );
    expect(missingWhy({ jira_ticket: 'ABC-1' })).toMatch(/paste the GitHub Actions run that built it/);
    expect(missingWhy({ build_run_url: RUN })).toBe('Never went through the queue — add its JIRA ticket, then tick');
    expect(noneWhy({ items: [item({ in_queue: true })] })).toBe(
      'Every chart in this release is already in the next release',
    );
    expect(noneWhy({ items: [item()] })).toMatch(/^None can go straight back/);
  });

  it('label the submit button and the empty list', () => {
    expect(addLabel(0, 0)).toBe('Add selected to the next release');
    expect(addLabel(2, 0)).toBe('Add 2 to the next release');
    expect(addLabel(3, 1)).toBe('Add 3 to the next release (1 hidden by the filter)');
    expect(emptyListText('  nope ', 'all')).toBe('No chart matches “nope” in the last 3 weeks.');
    expect(emptyListText('', 'DF')).toBe('No DF chart shipped in the last 3 weeks.');
  });
});
