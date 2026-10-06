// A port of tests/js/bq_cost.test.mjs and tests/js/monitoring.test.mjs — the
// same cases, so the plugin and the portal say the same words.
import {
  ENABLE_HINT,
  PARTIAL_LEAD,
  accessLines,
  checkExplainPrompt,
  costSummary,
  emptyText,
  extrasText,
  formatBytes,
  formatCount,
  formatGb,
  formatUsd,
  formatValue,
  historyText,
  insightText,
  monitoringSummary,
  orderChecks,
  orderShapes,
  reportMeta,
  seriesLabel,
  shapeExplainPrompt,
  watchingText,
  whoText,
} from './monitoringFormat';

const GIB = 1024 * 1024 * 1024;

describe('BigQuery cost report wording', () => {
  it('the summary reads the totals and what the report itself cost', () => {
    expect(
      costSummary({
        ok: true,
        totals: { queries: 1240, slot_hours: 612, gb_billed: 84.2 },
        report_cost_bytes: 0.4 * GIB,
      }),
    ).toBe(
      '1,240 queries · 612 slot-hours · 84.2 GB billed · this report read 0.4 GB',
    );
    expect(
      costSummary({
        ok: true,
        totals: { queries: 1, cache_hit_pct: 12.5 },
        report_cost_bytes: 42559434,
      }),
    ).toBe('1 query · 12.5% from cache · this report read 40.6 MB');
    expect(costSummary({ ok: true, totals: { queries: 3 } })).toBe('3 queries');
    expect(costSummary({ ok: true })).toBe('');
  });

  it('a disabled or failed report says so instead of showing zeros', () => {
    expect(
      costSummary({
        ok: false,
        disabled: true,
        error: 'BigQuery cost report is disabled (BQ_COST_REGION unset).',
      }),
    ).toBe('BigQuery cost report is disabled');
    // the error itself is shown once, in the body — the heading only says it failed
    expect(
      costSummary({ ok: false, error: '403: missing bigquery.jobs.listAll' }),
    ).toBe('BigQuery cost report is unavailable');
    expect(costSummary({ ok: false })).toBe(
      'BigQuery cost report is unavailable',
    );
    expect(costSummary(null)).toBe('no report');
    expect(ENABLE_HINT).toContain('BQ_COST_REGION');
    expect(ENABLE_HINT).toContain('BQ_COST_PROJECT');
  });

  it('shapes rank by slot-hours on reservations and by bytes billed on-demand, stably', () => {
    const shapes = [
      { qhash: 'a', gb_billed: 1, slot_hours: 9 },
      { qhash: 'b', gb_billed: 5, slot_hours: 1 },
      { qhash: 'c', gb_billed: 5 },
      { qhash: 'd' },
    ];
    expect(orderShapes(shapes, 'on-demand').map(s => s.qhash)).toEqual([
      'b',
      'c',
      'a',
      'd',
    ]);
    expect(orderShapes(shapes, 'reservations').map(s => s.qhash)).toEqual([
      'a',
      'b',
      'c',
      'd',
    ]);
    expect(orderShapes(shapes, undefined).map(s => s.qhash)).toEqual([
      'b',
      'c',
      'a',
      'd',
    ]);
    expect(orderShapes(null, 'on-demand')).toEqual([]);
    expect(orderShapes('not a list', 'on-demand')).toEqual([]);
  });

  it('byte counts read in bytes, KB, MB or up', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(21557)).toBe('21.1 KB');
    expect(formatBytes(13200000)).toBe('12.6 MB');
    expect(formatBytes(2 * GIB)).toBe('2.0 GB');
    expect(formatBytes(null)).toBe('—');
  });

  it('sizes take the unit that reads best', () => {
    expect(formatGb(0.0123)).toBe('12.6 MB');
    expect(formatGb(0.4)).toBe('0.4 GB');
    expect(formatGb(2.1)).toBe('2.1 GB');
    expect(formatGb(84.2)).toBe('84.2 GB');
    expect(formatGb(380.1)).toBe('380 GB');
    expect(formatGb(18432)).toBe('18.0 TB');
    expect(formatGb(1.5 * 1024 * 1024)).toBe('1.5 PB');
    expect(formatGb(0)).toBe('0 GB');
    expect(formatGb(null)).toBe('—');
    expect(formatGb('n/a')).toBe('—');
  });

  it('dollars carry two decimals and thousands separators; a monthly figure can be whole', () => {
    expect(formatUsd(0.01)).toBe('$0.01');
    expect(formatUsd(112.5)).toBe('$112.50');
    expect(formatUsd(1234.5)).toBe('$1,234.50');
    expect(formatUsd(82, 0)).toBe('$82');
    expect(formatUsd(-5)).toBe('-$5.00');
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd(undefined)).toBe('—');
  });

  it('counts group thousands, in every locale', () => {
    expect(formatCount(1240)).toBe('1,240');
    expect(formatCount(1234567)).toBe('1,234,567');
    expect(formatCount(-1500)).toBe('-1,500');
    expect(formatCount(null)).toBe('—');
  });

  it('who ran a shape is the short name, plus how many others', () => {
    expect(
      whoText({
        who: 'alice@example.com',
        users: ['alice@example.com', 'bob@example.com', 'carol@example.com'],
      }),
    ).toBe('alice +2');
    expect(whoText({ who: 'alice@example.com' })).toBe('alice');
    expect(whoText({ users: ['bob@example.com'] })).toBe('bob');
    // the server's distinct count wins over the capped sample of emails
    expect(
      whoText({
        who: 'alice@example.com',
        users: ['alice@example.com', 'bob@example.com'],
        users_count: 7,
      }),
    ).toBe('alice +6');
    expect(whoText({ who: 'alice@example.com', users_count: 1 })).toBe('alice');
    expect(whoText({})).toBe('');
  });

  it('performance insights are said in words, once each', () => {
    expect(insightText(['slot_contention'])).toBe('slot contention');
    expect(
      insightText([
        'slot_contention',
        'insufficient_shuffle_quota',
        'slot_contention',
      ]),
    ).toBe('slot contention, insufficient shuffle quota');
    expect(insightText([{ name: 'input_data_change' }, null, ''])).toBe(
      'input data change',
    );
    expect(insightText([])).toBe('');
    expect(insightText(null)).toBe('');
  });

  it('asking the chat about a row names it, its cost, and the tools that prove a rewrite', () => {
    expect(
      shapeExplainPrompt({ qhash: 'abc123', runs: 96, gb_billed: 18432 }),
    ).toBe(
      'Why is BigQuery query shape abc123 expensive (96 runs, 18.0 TB billed)? Use bq_query_detail, ' +
        'look at the referenced tables, and propose a cheaper rewrite tested with bq_verify_rewrite.',
    );
    expect(shapeExplainPrompt({ qhash: 'x' })).toBe(
      'Why is BigQuery query shape x expensive? Use bq_query_detail, look at the referenced tables, ' +
        'and propose a cheaper rewrite tested with bq_verify_rewrite.',
    );
    expect(shapeExplainPrompt(null)).toContain('bq_verify_rewrite');
  });

  it('history counts adoptions from numbers or from the list', () => {
    expect(historyText({ adopted: 3, still_open: 2 })).toBe(
      '3 adopted · 2 still open',
    );
    expect(
      historyText({
        adoption: [
          { status: 'adopted' },
          { status: 'still_open' },
          { status: 'still_open' },
          { status: 'new' },
        ],
      }),
    ).toBe('1 adopted · 2 still open · 1 new');
    expect(historyText({ adoption: [{ status: 'toString' }] })).toBe('');
    expect(historyText({})).toBe('');
    expect(historyText(null)).toBe('');
  });

  it('what the table leaves to the Excel is said in one line', () => {
    expect(
      extrasText({
        storage: [{}, {}, {}],
        writes: [{}],
        history: { adopted: 2, still_open: 1 },
      }),
    ).toBe(
      'storage: 3 findings · writes: 1 finding · history: 2 adopted, 1 still open — in the Excel',
    );
    expect(
      extrasText({ storage: [], writes: [], writes_error: 'denied' }),
    ).toBe('writes: unavailable — in the Excel');
    expect(extrasText({ storage: [], writes: [] })).toBe('');
    expect(extrasText(null)).toBe('');
  });

  it("the card's small print names the scope, and an empty window says so", () => {
    expect(
      reportMeta({
        project: 'example-project',
        region: 'region-us',
        days: 14,
        billing: 'on-demand',
        scanned_at: '2026-09-19T10:00:00Z',
      }),
    ).toBe(
      'example-project · region-us · last 14 days · on-demand billing · scanned 2026-09-19T10:00:00Z',
    );
    expect(reportMeta({ days: 1 })).toBe('last 1 day');
    expect(reportMeta(null)).toBe('');
    expect(emptyText({ days: 14 })).toBe(
      'nothing significant in the last 14 days',
    );
    expect(emptyText({})).toBe('nothing significant in the window');
  });

  it('a partial report lists each missing role, where to grant it and what it unlocks', () => {
    const lines = accessLines({
      missing_access: [
        {
          role: 'roles/bigquery.resourceViewer',
          permission: 'bigquery.jobs.listAll',
          grant_on: 'project p',
          sections: ['Query costs', 'Table read counts'],
        },
      ],
    });
    expect(lines).toEqual([
      {
        role: 'roles/bigquery.resourceViewer',
        grantOn: 'project p',
        permission: 'bigquery.jobs.listAll',
        unlocks: 'Query costs; Table read counts',
        note: '',
      },
    ]);
    expect(PARTIAL_LEAD).toMatch(/Partial report/);
  });

  it('nothing missing, or an old report without the field, shows no access panel', () => {
    expect(accessLines({ missing_access: [] })).toEqual([]);
    expect(accessLines({})).toEqual([]);
    expect(accessLines(null)).toEqual([]);
  });

  it('query costs refused for want of a role is not "nothing significant"', () => {
    expect(emptyText({ shapes_error: '403', days: 14 })).toMatch(
      /needs more access/,
    );
    expect(emptyText({ days: 14 })).toMatch(/nothing significant/);
  });

  it('refused job history shows as unreadable, not as zero queries and zero cost', () => {
    const s = costSummary({
      ok: true,
      shapes_error: '403',
      report_cost_bytes: 1024,
      totals: { queries: 0, slot_hours: 0, gb_billed: 0, cache_hit_pct: 0 },
    });
    expect(s).toMatch(/query costs not readable/);
    expect(s).not.toMatch(/0 queries|slot-hours|billed/);
  });
});

describe('PromQL checks wording', () => {
  it('the summary names every state and never calls an unmeasured project OK', () => {
    expect(monitoringSummary({ checks: [] })).toBe('no checks configured');
    expect(monitoringSummary({ checks: [{ name: 'a', state: 'ok' }] })).toBe(
      'the check is OK',
    );
    expect(
      monitoringSummary({
        checks: [
          { name: 'a', state: 'ok' },
          { name: 'b', state: 'ok' },
        ],
      }),
    ).toBe('all 2 checks OK');
    expect(
      monitoringSummary({
        checks: [
          { name: 'a', state: 'firing' },
          { name: 'b', state: 'unknown' },
          { name: 'c', state: 'ok' },
        ],
      }),
    ).toBe('1 firing · 1 could not run · 1 OK');
    expect(
      monitoringSummary({
        checks: [
          { name: 'a', state: 'ok' },
          { name: 'b', state: 'no_data' },
          { name: 'c', state: 'no_data' },
        ],
      }),
    ).toBe('1 OK · 2 not measured here');
    expect(monitoringSummary(null)).toBe('no checks configured');
  });

  it('what needs a human comes first', () => {
    const order = orderChecks([
      { name: 'a', state: 'no_data' },
      { name: 'b', state: 'ok' },
      { name: 'c', state: 'firing' },
      { name: 'd', state: 'unknown' },
      { name: 'e', state: 'firing' },
    ]);
    expect(order.map(c => c.name)).toEqual(['c', 'e', 'd', 'b', 'a']);
  });

  it('a healthy check says how much it watched', () => {
    expect(watchingText({ watching: 17 })).toBe('watching 17');
    expect(watchingText({ watching: 0 })).toBe('');
    expect(watchingText({ watching: null })).toBe('');
  });

  it('series labels drop Prometheus bookkeeping', () => {
    expect(
      seriesLabel({ __name__: 'up', job: 'api', instance: 'a:9090' }),
    ).toBe('job=api, instance=a:9090');
    expect(seriesLabel({ __name__: 'up' })).toBe('up');
    expect(
      seriesLabel({
        service: 'aiplatform.googleapis.com',
        monitored_resource: 'consumed_api',
      }),
    ).toBe('service=aiplatform.googleapis.com');
    expect(seriesLabel({})).toBe('value');
  });

  it('values read cleanly', () => {
    expect(formatValue(63)).toBe('63');
    expect(formatValue(12.3456)).toBe('12.35');
    expect(formatValue(0.000034722)).toBe('3.47e-5');
    expect(formatValue(null)).toBe('—');
    expect(formatValue('NaN')).toBe('—');
  });

  it('asking the chat about a check names it and its query', () => {
    const p = checkExplainPrompt({
      name: 'Targets down',
      state: 'firing',
      query: 'up == 0',
    });
    expect(p).toContain('"Targets down"');
    expect(p).toContain('up == 0');
    expect(p).toContain('firing');
  });
});
