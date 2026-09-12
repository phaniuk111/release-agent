import { describeRefusal, shortName, timeAgo } from './queueFormat';

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
