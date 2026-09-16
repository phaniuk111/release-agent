import { controlsSummary, describeRefusal, queuedNotice, shortName, timeAgo } from './queueFormat';

describe('describeRefusal — why a row was not queued', () => {
  it('names a failed control and its job (it used to print "undefined")', () => {
    expect(
      describeRefusal({
        artifact: 'payments-api:3.1.0',
        reason: 'This build is NOT eligible for the release',
        failed_controls: ['RFTL deploy control'],
        failed_controls_detail: [{ control: 'RFTL deploy control', job: 'build' }],
        failed_steps: [{ name: 'Build image', job: 'build' }],
      }),
    ).toBe('control RFTL deploy control in job build failed; step Build image in job build failed');
  });

  it('names controls that have not passed yet', () => {
    expect(
      describeRefusal({
        error: 'not passed',
        open_controls: [{ control: 'RCTLDEF1', job: 'publish', status: 'queued' }],
      }),
    ).toBe('control RCTLDEF1 in job publish not passed yet (queued)');
  });

  it("falls back to the backend's own sentence", () => {
    expect(describeRefusal({ error: 'That run built orders-api-1.0.0, not payments-api:1.0.0.' })).toBe(
      'That run built orders-api-1.0.0, not payments-api:1.0.0.',
    );
    expect(describeRefusal({ reason: 'not eligible because…' })).toBe('not eligible because…');
    expect(describeRefusal({})).toBe('not eligible');
  });
});

describe('timeAgo / shortName', () => {
  const now = Date.parse('2026-09-12T12:00:00Z');
  it('reads like the portal', () => {
    expect(timeAgo('2026-09-12T11:59:50Z', now)).toBe('1m ago');
    expect(timeAgo('2026-09-12T09:00:00Z', now)).toBe('3h ago');
    expect(timeAgo('2026-09-10T12:00:00Z', now)).toBe('2d ago');
    expect(timeAgo(undefined, now)).toBe('');
    expect(timeAgo('nonsense', now)).toBe('');
    expect(shortName('dev@example.com')).toBe('dev');
  });
});

describe('controlsSummary — the release queue Controls column', () => {
  it('shows an allowed control as OPEN, by number, with what to do', () => {
    const open = controlsSummary({
      build_verified: true,
      allowed_failures: 'RCTLDEF0001691 - Peer review evidence in job build-deploy-publish',
    });
    expect(open.state).toBe('open');
    expect(open.label).toBe('1691 open');
    expect(open.title).toContain('close it manually');
  });

  it('names every allowed control', () => {
    expect(controlsSummary({ build_verified: true, allowed_failures: 'RCTLDEF0001691, RCTLDEF0000043 in job b' }).label)
      .toBe('1691, 43 open');
  });

  it('says all passed, or not checked', () => {
    expect(controlsSummary({ build_verified: true }).label).toBe('all passed');
    expect(controlsSummary({ build_verified: null }).label).toBe('not checked');
    expect(controlsSummary({}).state).toBe('unknown');
  });
});

describe('queuedNotice — what the Add dialog says', () => {
  it('names a control left open instead of claiming everything passed', () => {
    const text = queuedNotice([
      { artifact: 'payments-api:9.1.1', allowed_failures: ['RCTLDEF0001691 - Peer review evidence in job build'] },
    ]);
    expect(text).toContain('1691 open');
    expect(text).toContain('close it manually');
    expect(text).not.toContain('controls passed.');
  });

  it('says controls passed when nothing is open', () => {
    expect(queuedNotice([{ artifact: 'a:1' }, { artifact: 'b:2' }])).toBe(
      'Queued a:1, b:2 — build and controls passed.',
    );
  });
});
