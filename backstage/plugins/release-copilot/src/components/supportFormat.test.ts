// The Support tab's wording — the same cases as the portal's
// tests/js/support_triage.test.mjs and tests/js/support_feedback.test.mjs.
import {
  CAUSE_PROMPT,
  ENABLE_HINT,
  MAX_CAUSE,
  VERDICTS,
  actionLabel,
  askPrompt,
  asksForCause,
  emptyText,
  feedbackPayload,
  investigatePrompt,
  jobHref,
  metaLine,
  statsLine,
  summaryLine,
  thanksText,
} from './supportFormat';

describe('support triage wording', () => {
  it('the summary leads with what L1 must look at, or says all clear', () => {
    expect(
      summaryLine({
        ok: true,
        date_label: 'COB',
        business_date: '2026-10-02',
        counts: { items: 610, failed: 42, stuck: 3, missing: 5 },
      }),
    ).toBe('COB 2026-10-02 · 42 failed · 3 stuck · 5 missing of 610 runs');
    expect(summaryLine({ ok: true, business_date: '2026-10-02', counts: { items: 1 } })).toBe(
      'Business date 2026-10-02 · all clear · 1 run',
    );
    expect(summaryLine({ ok: false, disabled: true })).toBe('Support triage is disabled');
    expect(summaryLine({ ok: false, error: 'x' })).toBe('Support triage is unavailable');
    expect(summaryLine({ ok: true })).toBe('no runs in the dates read');
    expect(summaryLine(null)).toBe('no runs in the dates read');
  });

  it('each action has a name, and an unknown one reads as check', () => {
    expect(actionLabel('wait')).toBe('Wait for data');
    expect(actionLabel('retrigger')).toBe('Re-trigger once');
    expect(actionLabel('escalate')).toBe('Escalate');
    expect(actionLabel('reboot')).toBe('Check');
  });

  it('the small print says retries that recovered and whether a runbook is in use', () => {
    expect(
      metaLine({ ok: true, counts: { recovered: 2 }, previous_date: '2026-10-01', runbook_entries: 4 }),
    ).toBe('2 recovered after a retry · compared with 2026-10-01 · 4 runbook entries');
    expect(metaLine({ ok: true })).toMatch(/no runbook configured/);
    expect(metaLine({ ok: false })).toBe('');
  });

  it('a job id becomes a link only from an http(s) template', () => {
    expect(jobHref('https://console.example/jobs/{job_id}?p=x', 'job 1')).toBe(
      'https://console.example/jobs/job%201?p=x',
    );
    expect(jobHref('', 'j')).toBe('');
    expect(jobHref('https://console.example/jobs', 'j')).toBe('');
    expect(jobHref('javascript:alert(1)//{job_id}', 'j')).toBe('');
  });

  it('"Ask why" names the incident and the date', () => {
    expect(askPrompt({ title: '5 failed · upstream' }, { business_date: '2026-10-02' })).toBe(
      'Support triage for 2026-10-02: explain incident "5 failed · upstream" — why it most likely happened, ' +
        'how urgent it is by our priority policy, what L1 should do now, and the ticket note.',
    );
    expect(askPrompt(null, null)).toMatch(/incident "\?"/);
  });

  it('an empty date and the enable hint say what to do', () => {
    expect(emptyText({ empty: true, business_date: '2026-10-02' })).toBe(
      'No runs for 2026-10-02 in the control table.',
    );
    expect(emptyText({ empty: true })).toBe('No runs in the dates read.');
    expect(emptyText({})).toBe('Nothing for L1 on this date.');
    expect(ENABLE_HINT.includes('SUPPORT_TABLE') && ENABLE_HINT.includes('private')).toBe(true);
  });

  it('"Investigate" hands the skill the incident, its date, shared values and jobs', () => {
    const inc = {
      id: 'e-1a2b3c4d',
      title: '4 failed · process · Report REPORT-B',
      shared: { system: 'SYS-B', source: 'sys-b-fetcher', process: 'REPORT-B' },
      job_ids: ['j1', 'j2'],
    };
    const rep = {
      business_date: '2026-10-02',
      date_label: 'COB',
      labels: { system: 'Feed', source: 'Source', process: 'Report' },
    };
    expect(investigatePrompt(inc, rep)).toBe(
      'Investigate incident e-1a2b3c4d · COB 2026-10-02 · 4 failed · process · Report REPORT-B · Feed SYS-B · ' +
        'Source sys-b-fetcher · Report REPORT-B · jobs j1, j2. Why did it fail, and what should L1 do?',
    );
    expect(investigatePrompt(null, null)).toBe(
      'Investigate incident ? · ?. Why did it fail, and what should L1 do?',
    );
  });
});

const EVENT = {
  business_date: '2026-10-02',
  incident_id: 'e-0000aaaa',
  title: 'Feed A late',
  model: 'm-1',
  model_calls: 3,
  seconds: 12.5,
};

describe('"Was this right?" wording and request', () => {
  it('the three answers come in the order the buttons show them', () => {
    expect(VERDICTS).toEqual([
      { key: 'right', label: 'Right' },
      { key: 'direction', label: 'Right direction' },
      { key: 'wrong', label: 'Wrong' },
    ]);
  });

  it('only a not-quite-right answer asks for the actual cause', () => {
    expect(asksForCause('right')).toBe(false);
    expect(asksForCause('direction')).toBe(true);
    expect(asksForCause('wrong')).toBe(true);
    expect(CAUSE_PROMPT).toBe('What was the actual cause?');
    expect(MAX_CAUSE).toBe(500);
  });

  it('the payload carries the investigation and the answer, and the cause as typed', () => {
    expect(feedbackPayload(EVENT, 'wrong', '  The vendor re-sent a file  ')).toEqual({
      business_date: '2026-10-02',
      incident_id: 'e-0000aaaa',
      title: 'Feed A late',
      verdict: 'wrong',
      actual_cause: 'The vendor re-sent a file',
      category: '',
      action: '',
      model: 'm-1',
      model_calls: 3,
      seconds: 12.5,
    });
  });

  it('a right answer never sends a cause, even from a half-typed box', () => {
    expect(feedbackPayload(EVENT, 'right', 'left over text').actual_cause).toBe('');
  });

  it('the payload never names who answered — the server knows', () => {
    const p = feedbackPayload(
      { ...EVENT, actor: 'x@example.com', requested_by: 'y@example.com' },
      'right',
      '',
    );
    expect('actor' in p).toBe(false);
    expect('requested_by' in p).toBe(false);
  });

  it('missing fields never throw, and absent timings are left out', () => {
    const p = feedbackPayload(null, 'direction', undefined);
    expect(p.business_date).toBe('');
    expect(p.incident_id).toBe('');
    expect(p.actual_cause).toBe('');
    expect('model_calls' in p).toBe(false);
    expect('seconds' in p).toBe(false);
    expect(feedbackPayload({ model_calls: 0, seconds: 0 }, 'right').model_calls).toBe(0);
  });

  it('each answer is thanked in its own words', () => {
    expect(thanksText('right')).toBe('Thanks — marked right.');
    expect(thanksText('direction')).toMatch(/right direction/);
    expect(thanksText('wrong')).toMatch(/runbook/);
    expect(thanksText('???')).toBe('Thanks for the feedback.');
    expect(thanksText(undefined)).toBe('Thanks for the feedback.');
  });

  it('the stats line says how many were rated and how often the agent was right', () => {
    expect(
      statsLine({
        ok: true,
        days: 30,
        total: 42,
        right: 23,
        direction: 13,
        accuracy: 23 / 42,
        right_or_direction: 36 / 42,
      }),
    ).toBe('30 days: 42 investigations rated · 55% right · 86% right or right direction');
    expect(statsLine({ ok: true, days: 7, total: 1, accuracy: 1, right_or_direction: 1 })).toBe(
      '7 days: 1 investigation rated · 100% right · 100% right or right direction',
    );
  });

  it('the stats line works from the counts when the ratios are missing', () => {
    expect(statsLine({ ok: true, days: 30, total: 4, right: 2, direction: 1, wrong: 1 })).toBe(
      '30 days: 4 investigations rated · 50% right · 75% right or right direction',
    );
  });

  it('the stats line copes with nothing rated, a disabled memory, and no payload', () => {
    expect(statsLine({ ok: true, days: 30, total: 0, accuracy: null })).toBe(
      '30 days: no investigations rated yet',
    );
    expect(statsLine({ ok: true })).toBe('30 days: no investigations rated yet');
    expect(statsLine({ ok: false, disabled: true })).toBe('Feedback is not being kept');
    expect(statsLine({ ok: false, error: 'x' })).toBe('Feedback is unavailable');
    expect(statsLine(null)).toBe('30 days: no investigations rated yet');
    expect(statsLine({ ok: true, days: 1, total: 0 })).toBe('1 day: no investigations rated yet');
  });
});
