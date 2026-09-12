import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  IconButton,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from '@material-ui/core';
import AddIcon from '@material-ui/icons/Add';
import RefreshIcon from '@material-ui/icons/Refresh';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';
import { describeRefusal, Refusal, shortName, timeAgo } from './queueFormat';

// The last email typed here, so withdrawing and queueing do not ask twice.
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

type QueueItem = {
  artifact_name?: string;
  artifact_version?: string;
  requested_by?: string;
  requested_at?: string;
  build_verified?: boolean | null;
  build_run_url?: string;
  note?: string;
  prl1_only?: boolean;
  df_only?: boolean;
  target_envs?: string;
  jira_ticket?: string;
  change_details?: string;
};

/**
 * The four ticks a developer sets per image, and how they reach the two
 * booleans the event log actually stores.
 *
 * partition_environments() in release_fileset.py is the authority on what they
 * mean:
 *   deployable = names - df_images        DF images enter NO helm environment
 *   prd        = deployable - prl1_only   prl1_only never reaches PRD
 *   uat/prl1   = deployable               everything else goes to both
 *
 * For the release FILE-SET, PRD therefore covers PRL1 — the restriction is
 * what PRL1-without-PRD encodes. The full selection travels separately as
 * target_envs, because the deploy-time choice of pipeline (DF especially) needs
 * "PRD + PRL1" and "PRD only" to stay distinguishable.
 */
type Ticks = {
  prd: boolean;
  prl1: boolean;
  care: boolean;
  df: boolean;
};

function toFlags(t: Ticks) {
  return {
    df_only: t.df,
    // Only a PRL1 tick WITHOUT PRD restricts the image. Same mapping as the
    // portal's own queue form, for both lanes.
    prl1_only: t.prl1 && !t.prd,
    // The full selection. prl1_only cannot carry it: "PRD + PRL1" and "PRD
    // only" both mean "not PRL1-only", and for a DF image — whose PRD and PRL1
    // pipelines are chosen at deploy time — that is exactly the difference
    // the deploy needs.
    target_envs: [t.prd && 'prd', t.prl1 && 'prl1'].filter(Boolean).join(','),
  };
}

/**
 * Combinations the release model cannot represent in one queued row. Reported
 * inline instead of being silently resolved, because either resolution would
 * ship the image somewhere the developer did not choose.
 */
function tickError(t: Ticks): string {
  if (t.care && t.df) {
    return 'CARE and DF are different release models — tick one. An image is either a helm chart in the CARE release or a Dataflow image, not both.';
  }
  if (!t.care && !t.df) return 'Tick CARE or DF.';
  if (t.care && !t.prd && !t.prl1) return 'Tick PRD and/or PRL1 for a CARE image.';
  return '';
}

/** The destination those ticks actually produce. */
function describeTicks(t: Ticks): string {
  const err = tickError(t);
  if (err) return '';
  if (t.df) {
    const picked = [t.prd && 'PRD', t.prl1 && 'PRL1'].filter(Boolean);
    return picked.length
      ? `Dataflow image — ${picked.join(' + ')} pipeline${picked.length > 1 ? 's' : ''}, triggered at deploy time.`
      : 'Dataflow image — built and deployed by the DF workflow.';
  }
  if (t.prd && t.prl1) return 'Goes to UAT, PRL1 and PRD.';
  // prl1_only has two states, so PRD alone records the pipeline to trigger —
  // it does not hold the chart out of the PRL1 file-set.
  if (t.prd) return 'PRD pipeline — release files still cover PRL1.';
  return 'Goes to UAT and PRL1 — held back from PRD.';
}

/** How a queued row reads back in the table. */
function describeDestination(prl1Only?: boolean, dfOnly?: boolean, targetEnvs?: string): string {
  const envs = (targetEnvs || '').split(',').filter(Boolean).map(e => e.toUpperCase());
  if (dfOnly) return envs.length ? `DF → ${envs.join(', ')}` : 'DF (Dataflow)';
  return prl1Only ? 'CARE → UAT, PRL1' : 'CARE → UAT, PRL1, PRD';
}

type QueueCtx = {
  queue?: QueueItem[];
  known_charts?: string[];
  default_repo?: string;
  error?: string;
};

export function QueueTab() {
  const apiBase = useApiBase();
  const [ctx, setCtx] = useState<QueueCtx | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [rows, setRows] = useState<
    Array<{
      artifact: string;
      jira_ticket: string;
      build_run_url: string;
      prd: boolean;
      prl1: boolean;
      care: boolean;
      df: boolean;
    }>
  >([
    {
      artifact: '',
      jira_ticket: '',
      build_run_url: '',
      prd: true,
      prl1: true,
      care: true,
      df: false,
    },
  ]);
  const [requestedBy, setRequestedBy] = useState(savedEmail);
  // The row waiting for "yes, remove it" — removing is a real change to the release.
  const [removing, setRemoving] = useState<QueueItem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [changeDetails, setChangeDetails] = useState('');
  const [note, setNote] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCtx(await apiGet<QueueCtx>(apiBase, '/api/release-queue'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const addRow = useCallback(() => {
    setRows(prev => [
      ...prev,
      {
        artifact: '',
        jira_ticket: '',
        build_run_url: '',
        prd: true,
        prl1: true,
        care: true,
        df: false,
      },
    ]);
  }, []);

  const updateRow = useCallback(
    (idx: number, patch: Partial<(typeof rows)[number]>) => {
      setRows(prev => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
    },
    [],
  );

  const removeRow = useCallback((idx: number) => {
    setRows(prev => prev.filter((_, i) => i !== idx));
  }, []);

  const submitBatch = useCallback(async () => {
    setError(null);
    const valid = rows.filter(r => r.artifact.trim());
    if (!valid.length) {
      setError('Add at least one image:version row.');
      return;
    }
    if (!requestedBy.trim()) {
      setError('Your email (requested_by) is required.');
      return;
    }
    // The backend refuses rows without the Actions run that built the tag —
    // it verifies the build and RLFT/RFTL controls before queuing.
    if (valid.some(r => !r.build_run_url.trim())) {
      setError(
        'Build run URL is required for each image (e.g. https://github.com/<org>/<build-repo>/actions/runs/<id>) — the agent validates the build and its controls before queuing.',
      );
      return;
    }
    // The backend refuses a row without its JIRA ticket, and a change without
    // "what changed and why" — say so before a round trip, for every row at once.
    const missingJira = valid.filter(r => !r.jira_ticket.trim()).map(r => r.artifact.trim());
    if (missingJira.length) {
      setError(`JIRA ticket is required: ${missingJira.join(', ')}`);
      return;
    }
    if (!changeDetails.trim()) {
      setError('Say what changed and why — it drafts the change request on release day.');
      return;
    }
    const bad = valid
      .map(r => ({ artifact: r.artifact.trim(), err: tickError(r) }))
      .filter(x => x.err);
    if (bad.length) {
      setError(bad.map(x => `${x.artifact || 'row'}: ${x.err}`).join(' | '));
      return;
    }
    setSubmitting(true);
    try {
      const result = await apiPost<{
        ok?: boolean;
        error?: string;
        queued?: Array<{ artifact?: string }>;
        refused?: Refusal[];
        split?: boolean;
      }>(apiBase, '/api/release-queue/batch', {
        rows: valid.map(r => ({
          artifact: r.artifact.trim(),
          jira_ticket: r.jira_ticket.trim(),
          build_run_url: r.build_run_url.trim(),
          ...toFlags(r),
        })),
        requested_by: requestedBy.trim(),
        change_details: changeDetails.trim(),
        note: note.trim(),
      },
      { allowFailure: true });
      saveEmail(requestedBy.trim());
      const queued = result.queued ?? [];
      const refused = result.refused ?? [];
      if (!queued.length && !refused.length) {
        throw new Error(result.error || 'Queueing refused by the backend.');
      }
      if (refused.length) {
        // PARTIAL success still reports ok:true — the refused rows must stay in
        // front of the developer (with why), or one change quietly ships split.
        const why = refused.map(r => `${r.artifact}: ${describeRefusal(r)}`).join(' | ');
        setError(
          (queued.length
            ? `Queued ${queued.map(q => q.artifact).join(', ')} — but NOT: `
            : 'Nothing queued: ') +
            why +
            (queued.length ? ' — this splits your change; fix and re-queue before release day.' : ''),
        );
        const refusedNames = new Set(refused.map(r => r.artifact));
        setRows(prev => prev.filter(r => refusedNames.has(r.artifact.trim())));
        await refresh();
        return;
      }
      setNotice(`Queued ${queued.map(q => q.artifact).join(', ')} — build and controls passed.`);
      setDialogOpen(false);
      setRows([
        {
          artifact: '',
          jira_ticket: '',
          build_run_url: '',
          prd: true,
          prl1: true,
          care: true,
          df: false,
        },
      ]);
      setChangeDetails('');
      setNote('');
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [rows, requestedBy, changeDetails, note, refresh, apiBase]);

  // Sends the version that was shown: if someone re-queued the chart at a new
  // version meanwhile, the backend refuses rather than removing theirs.
  const withdraw = useCallback(async () => {
    if (!removing) return;
    const who = requestedBy.trim();
    if (!who.includes('@')) {
      setError('Your email is needed — the removal is recorded against it.');
      return;
    }
    setError(null);
    const label = `${removing.artifact_name}:${removing.artifact_version}`;
    try {
      await apiPost(apiBase, '/api/release-queue/withdraw', {
        artifact_name: label,
        requested_by: who,
      });
      saveEmail(who);
      setNotice(`Removed ${label} from the next release.`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRemoving(null);
      await refresh();
    }
  }, [removing, requestedBy, refresh, apiBase]);

  const items = ctx?.queue ?? [];

  return (
    <Card>
      <CardHeader
        title="Release intake queue"
        subheader="Next-release items; queued notes go to DevOps"
        action={
          <>
            <IconButton
            aria-label="Refresh the queue"
            title="Refresh the queue"
            onClick={refresh}
            disabled={loading}
            size="small"
          >
              <RefreshIcon />
            </IconButton>
            <Button
              size="small"
              variant="outlined"
              startIcon={<AddIcon />}
              onClick={() => {
                setError(null);
                setNotice(null);
                setDialogOpen(true);
              }}
            >
              Add items
            </Button>
          </>
        }
      />
      <CardContent>
        {loading && <Progress />}
        {error && !dialogOpen && <Typography color="error">{error}</Typography>}
        {notice && !error && (
          <Typography style={{ color: '#34d399' }}>{notice}</Typography>
        )}
        {!loading && !items.length && !error && !ctx?.error && (
          <Typography color="textSecondary">Nothing is queued for the next release.</Typography>
        )}
        {items.length > 0 && (
          // Scrolls sideways on a narrow screen instead of breaking words
          // mid-way ("verifie|d", "Withdra|w").
          <div style={{ overflowX: 'auto' }}>
          <Table size="small" style={{ whiteSpace: 'nowrap' }}>
            <TableHead>
              <TableRow>
                <TableCell>Image</TableCell>
                <TableCell>Destination</TableCell>
                <TableCell>Jira</TableCell>
                <TableCell>Build</TableCell>
                <TableCell>Queued by</TableCell>
                <TableCell>Note</TableCell>
                <TableCell align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((it, idx) => (
                <TableRow key={idx}>
                  <TableCell style={{ fontFamily: 'ui-monospace, monospace' }}>
                    {it.artifact_name}:{it.artifact_version}
                  </TableCell>
                  <TableCell>
                    {describeDestination(it.prl1_only, it.df_only, it.target_envs)}
                  </TableCell>
                  <TableCell>{it.jira_ticket || '—'}</TableCell>
                  <TableCell>
                    <span style={{ color: it.build_verified ? '#34d399' : '#fbbf24' }}>
                      {it.build_verified ? '✓ verified' : '⚠ not verified'}
                    </span>
                    {it.build_run_url && (
                      <>
                        {' '}
                        <a href={it.build_run_url} target="_blank" rel="noopener noreferrer">
                          run
                        </a>
                      </>
                    )}
                  </TableCell>
                  <TableCell title={`${it.requested_by ?? ''} ${it.requested_at ?? ''}`}>
                    {shortName(it.requested_by) || '—'}
                    <Typography variant="caption" display="block" color="textSecondary">
                      {timeAgo(it.requested_at)}
                    </Typography>
                  </TableCell>
                  <TableCell style={{ whiteSpace: 'normal', minWidth: 160 }}>{it.note}</TableCell>
                  <TableCell align="right">
                    <Button size="small" color="secondary" onClick={() => setRemoving(it)}>
                      Withdraw
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          </div>
        )}
        {!ctx && !error && !loading && (
          <Typography color="textSecondary">
            Click refresh to load the queue.
          </Typography>
        )}
        {ctx?.error && !error && (
          <Typography color="error">{ctx.error}</Typography>
        )}

        <Dialog open={!!removing} onClose={() => setRemoving(null)} maxWidth="xs" fullWidth>
          <DialogTitle>Remove from the next release?</DialogTitle>
          <DialogContent>
            <Typography style={{ marginBottom: 12 }}>
              <b style={{ fontFamily: 'ui-monospace, monospace' }}>
                {removing?.artifact_name}:{removing?.artifact_version}
              </b>{' '}
              leaves the queue. It stays in the history.
            </Typography>
            <TextField
              fullWidth
              variant="outlined"
              size="small"
              label="Your email (recorded with the removal)"
              value={requestedBy}
              onChange={e => setRequestedBy(e.target.value)}
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setRemoving(null)}>Cancel</Button>
            <Button variant="contained" color="secondary" onClick={withdraw}>
              Remove
            </Button>
          </DialogActions>
        </Dialog>

        <Dialog
          open={dialogOpen}
          onClose={() => setDialogOpen(false)}
          fullWidth
          maxWidth="md"
        >
          <DialogTitle>Add to next release</DialogTitle>
          <DialogContent>
            {error && (
              <Typography color="error" style={{ marginBottom: 12 }}>
                {error}
              </Typography>
            )}
            <TextField
              id="queue-requested-by"
              fullWidth
              variant="outlined"
              size="small"
              label="Your email (requested_by)"
              style={{ marginBottom: 12 }}
              value={requestedBy}
              onChange={e => setRequestedBy(e.target.value)}
            />
            <TextField
              id="queue-change-details"
              fullWidth
              multiline
              minRows={3}
              variant="outlined"
              size="small"
              label="What changed and why (goes to the CHG draft)"
              style={{ marginBottom: 12 }}
              value={changeDetails}
              onChange={e => setChangeDetails(e.target.value)}
            />
            <TextField
              id="queue-note"
              fullWidth
              variant="outlined"
              size="small"
              label="Note to DevOps (optional)"
              style={{ marginBottom: 12 }}
              value={note}
              onChange={e => setNote(e.target.value)}
            />
            <Typography variant="subtitle2" gutterBottom>
              Images
            </Typography>
            {rows.map((r, idx) => (
              <div
                key={idx}
                style={{
                  border: '1px solid rgba(128,128,128,0.3)',
                  borderRadius: 4,
                  padding: 12,
                  marginBottom: 12,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    gap: 8,
                    alignItems: 'center',
                    marginBottom: 8,
                  }}
                >
                <TextField
                  id={`queue-image-${idx}`}
                  variant="outlined"
                  size="small"
                  label="image:version"
                  value={r.artifact}
                  onChange={e => updateRow(idx, { artifact: e.target.value })}
                  style={{ flex: 2 }}
                />
                <TextField
                  id={`queue-jira-${idx}`}
                  variant="outlined"
                  size="small"
                  label="Jira ticket"
                  value={r.jira_ticket}
                  onChange={e =>
                    updateRow(idx, { jira_ticket: e.target.value })
                  }
                  style={{ flex: 1 }}
                />
                <TextField
                  id={`queue-build-run-${idx}`}
                  variant="outlined"
                  size="small"
                  label="Build run URL (required — Actions run that built the tag)"
                  value={r.build_run_url}
                  onChange={e =>
                    updateRow(idx, { build_run_url: e.target.value })
                  }
                  style={{ flex: 2 }}
                />
                {rows.length > 1 && (
                  <Button size="small" onClick={() => removeRow(idx)}>
                    ✕
                  </Button>
                )}
                </div>
                <div
                  style={{
                    display: 'flex',
                    gap: 4,
                    alignItems: 'center',
                    flexWrap: 'wrap',
                  }}
                >
                  {(
                    [
                      ['prd', 'PRD'],
                      ['prl1', 'PRL1'],
                      ['care', 'CARE'],
                      ['df', 'DF'],
                    ] as Array<[keyof Ticks, string]>
                  ).map(([key, label]) => (
                    <FormControlLabel
                      key={key}
                      control={
                        <Checkbox
                          size="small"
                          id={`queue-${key}-${idx}`}
                          checked={r[key]}
                          onChange={e =>
                            updateRow(idx, { [key]: e.target.checked })
                          }
                        />
                      }
                      label={label}
                    />
                  ))}
                  <Typography
                    variant="caption"
                    color={tickError(r) ? 'error' : 'textSecondary'}
                    style={{ marginLeft: 8 }}
                  >
                    {tickError(r) || describeTicks(r)}
                  </Typography>
                </div>
              </div>
            ))}
            <Button size="small" onClick={addRow}>
              + Add another image
            </Button>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setDialogOpen(false)}>Cancel</Button>
            <Button
              variant="contained"
              color="primary"
              onClick={submitBatch}
              disabled={submitting}
            >
              {submitting ? 'Queueing…' : 'Queue items'}
            </Button>
          </DialogActions>
        </Dialog>
      </CardContent>
    </Card>
  );
}
