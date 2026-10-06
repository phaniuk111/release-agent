import { AREAS, OPS_AREAS, OPS_VIEWS, areaOf, resolve, visibleAreas } from './navigation';

describe('navigation', () => {
  it('splits release work and operations into two pages, every screen once', () => {
    expect(AREAS.map(a => a.label)).toEqual(['Ask', 'Ship', 'Queue']);
    expect(OPS_VIEWS).toEqual(['support', 'monitoring', 'insights']);
    const all = [...AREAS, ...OPS_AREAS].flatMap(a => a.views);
    expect(new Set(all).size).toBe(all.length);
    expect(all).toHaveLength(9);
  });

  it('hides a preview screen from a caller outside the preview, and an emptied area', () => {
    expect(visibleAreas(['Monitoring'], OPS_AREAS)[0].views).toEqual(['support', 'insights']);
    expect(visibleAreas([], OPS_AREAS)[0].views).toContain('monitoring');
    expect(visibleAreas(['Monitoring', 'Support'], [{ key: 'x', label: 'X', hint: '', views: ['monitoring'] }])).toEqual([]);
  });

  it('resolves new links, old links and nonsense', () => {
    const areas = visibleAreas([]);
    expect(resolve(areas, 'ship', 'dataflow')).toMatchObject({ area: { key: 'ship' }, view: 'dataflow' });
    expect(resolve(areas, 'ship', null)).toMatchObject({ view: 'deploy' });
    // "queue" is an area AND a screen: the area wins, so its view= is honoured
    expect(resolve(areas, 'queue', 'history')).toMatchObject({ area: { key: 'queue' }, view: 'history' });
    expect(resolve(areas, 'queue', null).view).toBe('queue');
    // ?tab=history was a link to the screen itself
    expect(resolve(areas, 'history', null)).toMatchObject({ area: { key: 'queue' }, view: 'history' });
    expect(resolve(areas, 'nope', 'nope')).toMatchObject({ area: { key: 'ask' }, view: 'chat' });
    const ops = visibleAreas(['Monitoring'], OPS_AREAS);
    expect(resolve(ops, null, 'support').view).toBe('support');
    // a hidden screen falls back to its area's first one
    expect(resolve(ops, 'operate', 'monitoring').view).toBe('support');
  });

  it('finds the area of a screen', () => {
    expect(areaOf(visibleAreas([]), 'history')?.key).toBe('queue');
  });
});
