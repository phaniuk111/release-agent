import { KeyboardEvent, useCallback, useEffect, useId, useMemo, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  Checkbox,
  IconButton,
  TextField,
  Typography,
} from '@material-ui/core';
import RefreshIcon from '@material-ui/icons/Refresh';
import ListIcon from '@material-ui/icons/List';
import ChevronRightIcon from '@material-ui/icons/ChevronRight';
import ExpandMoreIcon from '@material-ui/icons/ExpandMore';
import DoneAllIcon from '@material-ui/icons/DoneAll';
import AddShoppingCartIcon from '@material-ui/icons/AddShoppingCart';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';
import { DEV_PORTAL } from '../look';
import { describeRefusal, Refusal, shortName, timeAgo } from './queueFormat';
import {
  addLabel,
  busyLabel,
  COULD_NOT_QUEUE,
  EMAIL_NEEDED,
  emptyListText,
  filterReleases,
  HISTORY_DAYS,
  HISTORY_HINT,
  HistoryItem,
  HistoryRelease,
  itemState,
  Kind,
  kindCount,
  kindTitle,
  missingWhy,
  NO_RELEASES,
  queueDestination,
  releasesCount,
  releaseSummary,
  requeueAllIndices,
  requeueAllTitle,
  requeuePlan,
  rowOrigin,
  safeRunHref,
  Typed,
  unavailableText,
  withTyped,
} from './historyFormat';

// ---- Release history ---------------------------------------------------------
// Past releases, newest first, each with the charts it shipped. The point of the
// tab is the tick box: when a release had to be redone, tick the charts that
// must ship again and they go straight back into the next release's queue —
// each qualified once, at that version, so the build/controls gate is not run
// again. Only a chart that never went through the queue takes the gate, with a
// run and a ticket given here. Nothing here deploys, releases or edits history.

// Shared with the queue tab, so queueing from either place does not ask twice.
const EMAIL_KEY = 'release-copilot:email';
const savedEmail = () => {
  try {
    return window.localStorage.getItem(EMAIL_KEY) ?? '';
  } catch {
    return '';
  }
};
const saveEmail = (email: string) => {
  try {
    window.localStorage.setItem(EMAIL_KEY, email);
  } catch {
    /* private mode: just not remembered */
  }
};

const KINDS: Array<[Kind, string]> = [
  ['all', 'All'],
  ['CARE', 'CARE'],
  ['DF', 'DF'],
];

type HistoryCtx = {
  ok?: boolean;
  disabled?: boolean;
  error?: string;
  releases?: HistoryRelease[];
};

type CallResult = {
  ok?: boolean;
  error?: string;
  queued?: Array<{ artifact?: string }>;
  refused?: Refusal[];
};

type Flash = { ok: boolean; lines: string[] };

const key = (ri: number, ii: number) => `${ri}:${ii}`;
const itemsOf = (rel?: HistoryRelease) => (rel && rel.items) || [];
const labelOf = (it: HistoryItem) => `${it.artifact_name}:${it.artifact_version || ''}`;

const chipStyle = (color: string, background: string, border?: string) => ({
  borderRadius: DEV_PORTAL.radius.pill,
  padding: '0 6px',
  fontSize: 10,
  color,
  background,
  border: border ? `1px solid ${border}` : undefined,
  whiteSpace: 'nowrap' as const,
});

export function HistoryTab({ onOpenQueue }: { onOpenQueue?: () => void } = {}) {
  const apiBase = useApiBase();
  const uid = useId();
  const [ctx, setCtx] = useState<HistoryCtx | null>(null);
  const [loading, setLoading] = useState(false);
  const [flash, setFlash] = useState<Flash | null>(null);
  // The search and the kind filter survive a refresh. Ticks, typed values and
  // open releases are keyed by position, and a refresh can add a release at
  // the top — so those start over.
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<Kind>('all');
  const [open, setOpen] = useState<Record<number, boolean>>({ 0: true });
  // While searching every hit is shown open; the person's own open/closed
  // choices are kept apart so clearing the search puts them back.
  const [searchClosed, setSearchClosed] = useState<Record<number, boolean>>({});
  const [picks, setPicks] = useState<Record<string, boolean>>({});
  const [typed, setTyped] = useState<Record<string, Typed>>({});
  const [email, setEmail] = useState(savedEmail);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    async (nextFlash: Flash | null) => {
      setLoading(true);
      try {
        // Three weeks: a release that has to be redone is days old, not months —
        // and a shorter window is a smaller BigQuery scan on every open.
        const data = await apiGet<HistoryCtx>(apiBase, `/api/release-history?days=${HISTORY_DAYS}`);
        setCtx({ releases: [], ...data });
      } catch (e) {
        setCtx({ ok: false, error: (e as Error).message });
      } finally {
        setOpen({ 0: true });
        setSearchClosed({});
        setPicks({});
        setTyped({});
        setFlash(nextFlash);
        setLoading(false);
      }
    },
    [apiBase],
  );

  useEffect(() => {
    load(null);
  }, [load]);

  const releases = useMemo(() => ctx?.releases ?? [], [ctx]);
  const shown = useMemo(() => filterReleases(releases, { query, kind }), [releases, query, kind]);
  const visible = useMemo(() => {
    const v = new Set<string>();
    shown.forEach(s => s.items.forEach(x => v.add(key(s.index, x.index))));
    return v;
  }, [shown]);

  // What the gate will see for a row: the record, with anything typed over it.
  const merged = (ri: number, ii: number) => withTyped(itemsOf(releases[ri])[ii], typed[key(ri, ii)]);
  const tickedKeys = Object.keys(picks).filter(k => picks[k]);
  const hidden = tickedKeys.filter(k => !visible.has(k)).length;
  const searching = !!query.trim();
  const isOpen = (ri: number) => (searching ? !searchClosed[ri] : !!open[ri]);
  const tickedIn = (ri: number) => itemsOf(releases[ri]).filter((_, ii) => picks[key(ri, ii)]).length;

  const changeQuery = (q: string) => {
    setQuery(q);
    setSearchClosed({}); // a new query shows every hit open
  };
  // In a search box with text, Escape only clears the search.
  const onSearchKey = (e: KeyboardEvent) => {
    if (e.key !== 'Escape' || !query) return;
    e.preventDefault();
    e.stopPropagation();
    changeQuery('');
  };

  const toggle = (ri: number) => {
    const next = !isOpen(ri);
    if (searching) setSearchClosed(s => ({ ...s, [ri]: !next }));
    else setOpen(o => ({ ...o, [ri]: next }));
  };

  // Ticks only — the person still sees the rows and confirms with the one
  // "Add N" button below. Only rows the current search/filter shows.
  const requeueAll = (ri: number) => {
    const s = shown.find(x => x.index === ri);
    const picked = requeueAllIndices(releases[ri], s ? s.items.map(x => x.index) : []);
    setPicks(p => {
      const next = { ...p };
      picked.forEach(ii => {
        next[key(ri, ii)] = true;
      });
      return next;
    });
    setOpen(o => ({ ...o, [ri]: true }));
    setSearchClosed(c => ({ ...c, [ri]: false }));
  };

  // A row missing what the gate will ask for can be ticked once it has been
  // given: a real run URL, and a ticket.
  const typeInto = (ri: number, ii: number, field: 'run' | 'jira', value: string) => {
    const k = key(ri, ii);
    const t = { ...(typed[k] || {}), [field]: value };
    setTyped(prev => ({ ...prev, [k]: t }));
    if (itemState(withTyped(itemsOf(releases[ri])[ii], t)) !== 'ready') {
      setPicks(p => ({ ...p, [k]: false }));
    }
  };

  const submit = async () => {
    const who = email.trim();
    if (!who.includes('@')) {
      setErr(EMAIL_NEEDED);
      return;
    }
    // Every tick counts, including rows the current filter hides.
    const chosen = tickedKeys.map(k => {
      const [ri, ii] = k.split(':').map(Number);
      return { rel: releases[ri], it: merged(ri, ii) };
    });
    const { direct, gated, skipped } = requeuePlan(chosen.map(c => c.it));
    if (!direct.length && !gated.length) {
      setErr(skipped.map(s => `${s.artifact}: ${s.reason}`).join('; '));
      return;
    }
    saveEmail(who);
    setBusy(busyLabel(gated.length));
    setErr('');
    const from = [...new Set(chosen.map(c => c.rel.release_name))].join(', ');
    const fail = (e: unknown): CallResult => ({ ok: false, error: String((e as Error)?.message || e) });
    // Charts that qualified once go straight back; never-queued ones take the
    // gate, carrying their own details (the shared line is only for one with none).
    const [back, checked] = await Promise.all([
      direct.length
        ? apiPost<CallResult>(
            apiBase,
            '/api/release-queue/requeue',
            { requested_by: who, items: direct },
            { allowFailure: true },
          ).catch(fail)
        : null,
      gated.length
        ? apiPost<CallResult>(
            apiBase,
            '/api/release-queue/batch',
            { requested_by: who, change_details: `Re-queued from ${from}`, rows: gated },
            { allowFailure: true },
          ).catch(fail)
        : null,
    ]);
    const queued = [...(back?.queued ?? []), ...(checked?.queued ?? [])];
    // A call that failed outright answered for none of its charts — name each
    // one, or a failure beside another call's success reads as nothing at all.
    const failedCall = (res: CallResult | null, artifacts: string[]): Refusal[] =>
      res && res.error && !res.queued && !res.refused ? artifacts.map(artifact => ({ artifact, error: res.error })) : [];
    const refused: Refusal[] = [
      ...(back?.refused ?? []),
      ...(checked?.refused ?? []),
      ...(queued.length ? failedCall(back, direct.map(d => `${d.artifact_name}:${d.artifact_version}`)) : []),
      ...(queued.length ? failedCall(checked, gated.map(g => g.artifact)) : []),
    ];
    if (!queued.length && !refused.length) {
      const failed = [back, checked].find(r => r && r.error);
      setBusy(null);
      setErr(failed?.error || COULD_NOT_QUEUE);
      return;
    }
    const lines = [
      ...queued.map(q => `✅ ${q.artifact} is back in the next release.`),
      ...refused.map(r => `❌ ${r.artifact} — ${describeRefusal(r)}`),
      ...skipped.map(s => `⏭ ${s.artifact} — ${s.reason}`),
    ];
    await load({ ok: !refused.length, lines });
    setBusy(null);
  };

  const failed = ctx?.ok === false;

  const renderRow = (ri: number, ii: number, first: boolean) => {
    const it = itemsOf(releases[ri])[ii];
    const k = key(ri, ii);
    // The record decides the layout; the typed values decide the tick.
    const state = itemState(it);
    const m = merged(ri, ii);
    const ok = itemState(m) === 'ready';
    const label = labelOf(it);
    const detail = [it.change_details, it.note].filter(Boolean).join(' · ');
    const why = state === 'in-queue' ? 'Already queued for the next release' : ok ? '' : missingWhy(m);
    const rowTitle = [rowOrigin(it), state === 'needs-input' ? detail : ''].filter(Boolean).join('\n');
    const t = typed[k] || {};
    const href = safeRunHref(it.build_run_url);
    return (
      <div
        key={k}
        data-item={k}
        title={rowTitle || undefined}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          padding: '6px 0',
          borderTop: first ? undefined : `1px solid ${DEV_PORTAL.border}`,
        }}
      >
        <span title={why || undefined}>
          <Checkbox
            size="small"
            style={{ padding: 2 }}
            disabled={!ok || !!busy}
            checked={ok && !!picks[k]}
            onChange={e => setPicks(p => ({ ...p, [k]: e.target.checked }))}
            inputProps={{ 'aria-label': label }}
          />
        </span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6 }}>
            <span style={{ fontFamily: DEV_PORTAL.mono, color: DEV_PORTAL.text, fontSize: 12 }}>{label}</span>
            <span style={chipStyle(DEV_PORTAL.textMuted, 'transparent', DEV_PORTAL.borderStrong)}>
              {queueDestination(it)}
            </span>
            {state === 'in-queue' && (
              <span style={chipStyle(DEV_PORTAL.mint, 'rgba(16,185,129,0.15)')}>in next release</span>
            )}
            {state === 'needs-input' && (
              <span
                style={chipStyle('#fcd34d', 'rgba(245,158,11,0.15)')}
                title="Typed straight into a release form: the gate never ran for it, and it needs both to run now"
              >
                never queued — needs run + ticket
              </span>
            )}
          </div>
          {state === 'needs-input' ? (
            // No verified run on record — the gate still needs one and a ticket,
            // so the row takes them here and the tick enables once both are valid.
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
              <TextField
                size="small"
                variant="outlined"
                type="url"
                placeholder="paste the run that built it"
                value={t.run ?? it.build_run_url ?? ''}
                onChange={e => typeInto(ri, ii, 'run', e.target.value)}
                inputProps={{ 'aria-label': `Build run URL for ${label}`, style: { fontSize: 11, padding: '4px 8px' } }}
                style={{ flex: 1, minWidth: 200 }}
              />
              <TextField
                size="small"
                variant="outlined"
                placeholder="ticket"
                value={t.jira ?? it.jira_ticket ?? ''}
                onChange={e => typeInto(ri, ii, 'jira', e.target.value)}
                inputProps={{
                  'aria-label': `JIRA ticket for ${label}`,
                  style: { fontSize: 11, padding: '4px 8px', textTransform: 'uppercase' },
                }}
                style={{ width: 110 }}
              />
            </div>
          ) : (
            (it.jira_ticket || href || detail) && (
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  alignItems: 'center',
                  columnGap: 12,
                  marginTop: 2,
                  fontSize: 10,
                  color: DEV_PORTAL.textFaint,
                }}
              >
                {it.jira_ticket && <span style={{ color: DEV_PORTAL.amber }}>{it.jira_ticket}</span>}
                {href && (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="The GitHub Actions run that built it"
                    style={{ color: DEV_PORTAL.sky }}
                  >
                    run
                  </a>
                )}
                {detail && (
                  <span title={detail} style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {detail}
                  </span>
                )}
              </div>
            )
          )}
        </div>
      </div>
    );
  };

  const renderRelease = ({ rel, index: ri, items }: (typeof shown)[number]) => {
    const isOpenNow = isOpen(ri);
    const n = requeueAllIndices(rel, items.map(x => x.index)).length;
    const total = itemsOf(rel).length;
    const meta = [
      rel.pr_number ? `PR #${rel.pr_number}` : '',
      timeAgo(rel.released_at),
      rel.released_by ? `by ${shortName(rel.released_by)}` : '',
    ]
      .filter(Boolean)
      .join(' · ');
    const bodyId = `history-${uid}-${ri}`;
    const ticked = tickedIn(ri);
    const Chevron = isOpenNow ? ExpandMoreIcon : ChevronRightIcon;
    return (
      <div
        key={ri}
        data-rel={ri}
        style={{
          border: `1px solid ${DEV_PORTAL.borderStrong}`,
          borderRadius: 8,
          background: DEV_PORTAL.sunken,
          marginBottom: 8,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px' }}>
          <button
            type="button"
            aria-expanded={isOpenNow}
            aria-controls={bodyId}
            onClick={() => toggle(ri)}
            title={`${rel.release_name}${meta ? ` · ${meta}` : ''}`}
            style={{
              flex: 1,
              minWidth: 0,
              display: 'flex',
              alignItems: 'flex-start',
              gap: 6,
              textAlign: 'left',
              background: 'none',
              border: 0,
              padding: 0,
              cursor: 'pointer',
              color: 'inherit',
              font: 'inherit',
            }}
          >
            <Chevron style={{ fontSize: 16, color: DEV_PORTAL.textFaint, marginTop: 1 }} />
            <span style={{ minWidth: 0, flex: 1 }}>
              <span style={{ display: 'block', fontSize: 12 }}>
                <span style={{ fontWeight: 600, color: DEV_PORTAL.text }}>{rel.release_name}</span>
                {meta && <span style={{ color: DEV_PORTAL.textFaint }}> · {meta}</span>}
              </span>
              <span style={{ display: 'block', fontSize: 11, color: DEV_PORTAL.textMuted }}>
                {releaseSummary(rel)}
                {items.length < total && (
                  <span style={{ color: DEV_PORTAL.textFaint }}>
                    {' '}
                    · {items.length} of {total} shown
                  </span>
                )}
                {ticked > 0 && <span style={{ color: DEV_PORTAL.emeraldLight }}> · {ticked} ticked</span>}
              </span>
            </span>
          </button>
          {/* A sibling of the header, not inside it: ticking must not collapse. */}
          <span title={requeueAllTitle(n, rel)}>
            <Button
              size="small"
              disabled={!n || !!busy}
              startIcon={<DoneAllIcon fontSize="small" />}
              onClick={() => requeueAll(ri)}
              style={{ color: n ? DEV_PORTAL.emeraldLight : undefined, whiteSpace: 'nowrap', fontSize: 11 }}
            >
              Re-queue all ({n})
            </Button>
          </span>
        </div>
        {isOpenNow && (
          <div id={bodyId} style={{ borderTop: `1px solid ${DEV_PORTAL.border}`, padding: '0 8px' }}>
            {items.map((x, j) => renderRow(ri, x.index, j === 0))}
          </div>
        )}
      </div>
    );
  };

  return (
    <Card>
      <CardHeader
        title="Release history"
        subheader={ctx && !failed ? releasesCount(releases.length) : undefined}
        action={
          <>
            <IconButton
              aria-label="Refresh the history"
              title="Refresh"
              onClick={() => load(null)}
              disabled={loading || !!busy}
              size="small"
            >
              <RefreshIcon />
            </IconButton>
            {onOpenQueue && (
              <Button size="small" startIcon={<ListIcon />} onClick={onOpenQueue} style={{ color: DEV_PORTAL.emeraldLight }}>
                Open the queue
              </Button>
            )}
          </>
        }
      />
      <CardContent>
        {loading && <Progress />}
        {flash && (
          <div style={{ fontSize: 12, marginBottom: 8, color: flash.ok ? DEV_PORTAL.emeraldLight : DEV_PORTAL.amber }}>
            {flash.lines.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        )}
        {ctx && failed && <Typography color="textSecondary">{unavailableText(ctx)}</Typography>}
        {ctx && !failed && !releases.length && <Typography color="textSecondary">{NO_RELEASES}</Typography>}
        {ctx && !failed && releases.length > 0 && (
          <>
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginBottom: 8 }}>
              <TextField
                size="small"
                variant="outlined"
                type="search"
                placeholder="Search chart or ticket"
                value={query}
                onChange={e => changeQuery(e.target.value)}
                onKeyDown={onSearchKey}
                inputProps={{ 'aria-label': 'Search chart or ticket' }}
                style={{ flex: 1, minWidth: 180 }}
              />
              <div role="group" aria-label="Show which charts" style={{ display: 'flex', gap: 4 }}>
                {KINDS.map(([k, label]) => {
                  const on = kind === k;
                  const count = kindCount(releases, k);
                  return (
                    <Button
                      key={k}
                      size="small"
                      variant={on ? 'outlined' : 'text'}
                      aria-pressed={on}
                      title={kindTitle(k, count)}
                      onClick={() => setKind(k)}
                      style={{ borderRadius: DEV_PORTAL.radius.pill, minWidth: 0, fontSize: 11 }}
                    >
                      {label} <span style={{ color: DEV_PORTAL.textFaint, marginLeft: 4 }}>{count}</span>
                    </Button>
                  );
                })}
              </div>
            </div>
            <Typography variant="body2" style={{ color: DEV_PORTAL.textMuted, marginBottom: 8, fontSize: 12 }}>
              {HISTORY_HINT}
            </Typography>
            <div>
              {shown.length ? (
                shown.map(renderRelease)
              ) : (
                <Typography variant="body2" color="textSecondary">
                  {emptyListText(query, kind)}{' '}
                  {query.trim() ? (
                    <Button size="small" onClick={() => changeQuery('')}>
                      Clear search
                    </Button>
                  ) : (
                    <Button size="small" onClick={() => setKind('all')}>
                      Show all
                    </Button>
                  )}
                </Typography>
              )}
            </div>
            {/* Who is asking, and one button for every tick. */}
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginTop: 8 }}>
              <TextField
                size="small"
                variant="outlined"
                type="email"
                placeholder="your email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                inputProps={{ 'aria-label': 'Your email' }}
                style={{ width: 240 }}
              />
              <Button
                variant="contained"
                color="primary"
                size="small"
                startIcon={<AddShoppingCartIcon fontSize="small" />}
                disabled={!!busy || !tickedKeys.length}
                onClick={submit}
              >
                {busy || addLabel(tickedKeys.length, hidden)}
              </Button>
              {err && <span style={{ color: DEV_PORTAL.amber, fontSize: 12 }}>{err}</span>}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
