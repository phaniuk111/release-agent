// The BQ cost report pill's wording (static/core/bq_cost.js).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
    ENABLE_HINT, costSummary, emptyText, explainPrompt, extrasText, formatBytes, formatCount, formatGb, formatUsd,
    historyText, insightText, orderShapes, reportMeta, whoText,
} from '../../src/release_agent/static/core/bq_cost.js';

const GIB = 1024 * 1024 * 1024;

test('the summary reads the totals and what the report itself cost', () => {
    assert.equal(costSummary({ ok: true, totals: { queries: 1240, slot_hours: 612, gb_billed: 84.2 },
        report_cost_bytes: 0.4 * GIB }),
    '1,240 queries · 612 slot-hours · 84.2 GB billed · this report read 0.4 GB');
    assert.equal(costSummary({ ok: true, totals: { queries: 1, cache_hit_pct: 12.5 }, report_cost_bytes: 42559434 }),
        '1 query · 12.5% from cache · this report read 40.6 MB');
    assert.equal(costSummary({ ok: true, totals: { queries: 3 } }), '3 queries');
    assert.equal(costSummary({ ok: true }), '');
});

test('a disabled or failed report says so instead of showing zeros', () => {
    assert.equal(costSummary({ ok: false, disabled: true, error: 'BigQuery cost report is disabled (BQ_COST_REGION unset).' }),
        'BigQuery cost report is disabled');
    // the error itself is shown once, in the body — the heading only says it failed
    assert.equal(costSummary({ ok: false, error: '403: missing bigquery.jobs.listAll' }), 'BigQuery cost report is unavailable');
    assert.equal(costSummary({ ok: false }), 'BigQuery cost report is unavailable');
    assert.equal(costSummary(null), 'no report');
    assert.ok(ENABLE_HINT.includes('BQ_COST_REGION') && ENABLE_HINT.includes('BQ_COST_PROJECT'));
});

test('shapes rank by slot-hours on reservations and by bytes billed on-demand, stably', () => {
    const shapes = [{ qhash: 'a', gb_billed: 1, slot_hours: 9 }, { qhash: 'b', gb_billed: 5, slot_hours: 1 },
        { qhash: 'c', gb_billed: 5 }, { qhash: 'd' }];
    assert.deepEqual(orderShapes(shapes, 'on-demand').map(s => s.qhash), ['b', 'c', 'a', 'd']);
    assert.deepEqual(orderShapes(shapes, 'reservations').map(s => s.qhash), ['a', 'b', 'c', 'd']);
    assert.deepEqual(orderShapes(shapes, undefined).map(s => s.qhash), ['b', 'c', 'a', 'd']);
    assert.deepEqual(orderShapes(null, 'on-demand'), []);
    assert.deepEqual(orderShapes('not a list', 'on-demand'), []);
});

test('byte counts read in bytes, KB, MB or up', () => {
    assert.equal(formatBytes(0), '0 B');
    assert.equal(formatBytes(512), '512 B');
    assert.equal(formatBytes(21557), '21.1 KB');
    assert.equal(formatBytes(13200000), '12.6 MB');
    assert.equal(formatBytes(2 * GIB), '2.0 GB');
    assert.equal(formatBytes(null), '—');
});

test('sizes take the unit that reads best', () => {
    assert.equal(formatGb(0.0123), '12.6 MB');
    assert.equal(formatGb(0.4), '0.4 GB');
    assert.equal(formatGb(2.1), '2.1 GB');
    assert.equal(formatGb(84.2), '84.2 GB');
    assert.equal(formatGb(380.1), '380 GB');
    assert.equal(formatGb(18432), '18.0 TB');
    assert.equal(formatGb(1.5 * 1024 * 1024), '1.5 PB');
    assert.equal(formatGb(0), '0 GB');
    assert.equal(formatGb(null), '—');
    assert.equal(formatGb('n/a'), '—');
});

test('dollars carry two decimals and thousands separators; a monthly figure can be whole', () => {
    assert.equal(formatUsd(0.01), '$0.01');
    assert.equal(formatUsd(112.5), '$112.50');
    assert.equal(formatUsd(1234.5), '$1,234.50');
    assert.equal(formatUsd(82, 0), '$82');
    assert.equal(formatUsd(-5), '-$5.00');
    assert.equal(formatUsd(null), '—');
    assert.equal(formatUsd(undefined), '—');
});

test('counts group thousands, in every locale', () => {
    assert.equal(formatCount(1240), '1,240');
    assert.equal(formatCount(1234567), '1,234,567');
    assert.equal(formatCount(-1500), '-1,500');
    assert.equal(formatCount(null), '—');
});

test('who ran a shape is the short name, plus how many others', () => {
    assert.equal(whoText({ who: 'alice@example.com', users: ['alice@example.com', 'bob@example.com', 'carol@example.com'] }),
        'alice +2');
    assert.equal(whoText({ who: 'alice@example.com' }), 'alice');
    assert.equal(whoText({ users: ['bob@example.com'] }), 'bob');
    // the server's distinct count wins over the capped sample of emails
    assert.equal(whoText({ who: 'alice@example.com', users: ['alice@example.com', 'bob@example.com'], users_count: 7 }), 'alice +6');
    assert.equal(whoText({ who: 'alice@example.com', users_count: 1 }), 'alice');
    assert.equal(whoText({}), '');
});

test('performance insights are said in words, once each', () => {
    assert.equal(insightText(['slot_contention']), 'slot contention');
    assert.equal(insightText(['slot_contention', 'insufficient_shuffle_quota', 'slot_contention']),
        'slot contention, insufficient shuffle quota');
    assert.equal(insightText([{ name: 'input_data_change' }, null, '']), 'input data change');
    assert.equal(insightText([]), '');
    assert.equal(insightText(null), '');
});

test('asking the chat about a row names it, its cost, and the tools that prove a rewrite', () => {
    assert.equal(explainPrompt({ qhash: 'abc123', runs: 96, gb_billed: 18432 }),
        'Why is BigQuery query shape abc123 expensive (96 runs, 18.0 TB billed)? Use bq_query_detail, ' +
        'look at the referenced tables, and propose a cheaper rewrite tested with bq_verify_rewrite.');
    assert.equal(explainPrompt({ qhash: 'x' }),
        'Why is BigQuery query shape x expensive? Use bq_query_detail, look at the referenced tables, ' +
        'and propose a cheaper rewrite tested with bq_verify_rewrite.');
    assert.ok(explainPrompt(null).includes('bq_verify_rewrite'));
});

test('history counts adoptions from numbers or from the list', () => {
    assert.equal(historyText({ adopted: 3, still_open: 2 }), '3 adopted · 2 still open');
    assert.equal(historyText({ adoption: [{ status: 'adopted' }, { status: 'still_open' }, { status: 'still_open' }, { status: 'new' }] }),
        '1 adopted · 2 still open · 1 new');
    assert.equal(historyText({ adoption: [{ status: 'toString' }] }), '');
    assert.equal(historyText({}), '');
    assert.equal(historyText(null), '');
});

test('what the table leaves to the Excel is said in one line', () => {
    assert.equal(extrasText({ storage: [{}, {}, {}], writes: [{}], history: { adopted: 2, still_open: 1 } }),
        'storage: 3 findings · writes: 1 finding · history: 2 adopted, 1 still open — in the Excel');
    assert.equal(extrasText({ storage: [], writes: [], writes_error: 'denied' }), 'writes: unavailable — in the Excel');
    assert.equal(extrasText({ storage: [], writes: [] }), '');
    assert.equal(extrasText(null), '');
});

test('the card\'s small print names the scope, and an empty window says so', () => {
    assert.equal(reportMeta({ project: 'team-bq', region: 'region-us', days: 14, billing: 'on-demand', scanned_at: '2026-09-19T10:00:00Z' }),
        'team-bq · region-us · last 14 days · on-demand billing · scanned 2026-09-19T10:00:00Z');
    assert.equal(reportMeta({ days: 1 }), 'last 1 day');
    assert.equal(reportMeta(null), '');
    assert.equal(emptyText({ days: 14 }), 'nothing significant in the last 14 days');
    assert.equal(emptyText({}), 'nothing significant in the window');
});
