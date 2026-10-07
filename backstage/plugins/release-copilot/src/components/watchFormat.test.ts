import { asInvestigation, groupFindings, statusActions, statusText, watcherHealth } from './watchFormat';

const NOW = Date.parse('2026-10-07T12:00:00Z');
const minutesAgo = (m: number) => new Date(NOW - m * 60000).toISOString();

describe('groupFindings', () => {
  it('puts open high and critical first, then other open, then the closed', () => {
    const groups = groupFindings([
      { business_date: 'd', incident_id: 'low', status: 'open', priority: 'low' },
      { business_date: 'd', incident_id: 'done', status: 'resolved', priority: 'high' },
      { business_date: 'd', incident_id: 'hot', status: 'open', priority: 'high' },
    ]);
    expect(groups.map(g => [g.key, g.items.map(i => i.incident_id)])).toEqual([
      ['attention', ['hot']],
      ['open', ['low']],
      ['closed', ['done']],
    ]);
  });

  it('leaves out empty groups', () => {
    expect(groupFindings([])).toEqual([]);
  });
});

describe('watcherHealth', () => {
  it('is fine within its interval', () => {
    expect(watcherHealth({ enabled: true, interval_minutes: 15, last_run_at: minutesAgo(4) }, NOW)).toEqual({
      tone: 'ok',
      text: 'Ran 4 min ago',
      detail: 'Next in about 11 min',
    });
  });

  it('warns when it has missed two runs — a stopped watcher must not look quiet', () => {
    expect(watcherHealth({ enabled: true, interval_minutes: 15, last_run_at: minutesAgo(45) }, NOW).tone).toBe('warn');
  });

  it('shows the error of a failed run', () => {
    expect(watcherHealth({ enabled: true, interval_minutes: 15, last_error: 'no table' }, NOW)).toMatchObject({
      tone: 'error',
      detail: 'no table',
    });
  });

  it('says when it only runs on demand', () => {
    expect(watcherHealth({ enabled: false }, NOW).tone).toBe('off');
  });
});

describe('statuses', () => {
  it('offers resolve and dismiss on an open finding, reopen otherwise', () => {
    expect(statusActions('open').map(a => a.status)).toEqual(['resolved', 'dismissed']);
    expect(statusActions('cleared').map(a => a.status)).toEqual(['open']);
  });

  it('names who closed it, but not the watcher', () => {
    expect(statusText({ business_date: 'd', incident_id: 'a', status: 'resolved', actor: 'carol@example.com' })).toBe(
      'Resolved by carol@example.com',
    );
    expect(statusText({ business_date: 'd', incident_id: 'a', status: 'cleared', actor: 'watcher' })).toMatch(/^Cleared/);
  });

  it('records feedback against the same incident and model', () => {
    expect(
      asInvestigation({ business_date: 'd', incident_id: 'a', status: 'open', model: 'm', model_calls: 1, seconds: 3 }),
    ).toMatchObject({ business_date: 'd', incident_id: 'a', model: 'm', model_calls: 1, seconds: 3 });
  });
});
