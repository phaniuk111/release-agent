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

type QueueItem = {
  artifact_name?: string;
  artifact_version?: string;
  requested_by?: string;
  event_ts?: string;
  note?: string;
  prl1_only?: boolean;
  df_only?: boolean;
  jira_ticket?: string;
  change_details?: string;
};

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
      prl1_only: boolean;
      df_only: boolean;
    }>
  >([
    {
      artifact: '',
      jira_ticket: '',
      build_run_url: '',
      prl1_only: false,
      df_only: false,
    },
  ]);
  const [requestedBy, setRequestedBy] = useState('');
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
        prl1_only: false,
        df_only: false,
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
      setError('Add at least one chart:version row.');
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
        'Build run URL is required for each chart (e.g. https://github.com/<org>/<build-repo>/actions/runs/<id>) — the agent validates the build and its controls before queuing.',
      );
      return;
    }
    setSubmitting(true);
    try {
      const result = await apiPost<{
        ok?: boolean;
        refused?: Array<{ artifact?: string; error?: string }>;
      }>(apiBase, '/api/release-queue/batch', {
        rows: valid.map(r => ({
          artifact: r.artifact.trim(),
          jira_ticket: r.jira_ticket.trim(),
          build_run_url: r.build_run_url.trim(),
          prl1_only: r.prl1_only,
          df_only: r.df_only,
        })),
        requested_by: requestedBy.trim(),
        change_details: changeDetails.trim(),
        note: note.trim(),
      },
      { allowFailure: true });
      if (result.ok === false) {
        const reasons = (result.refused ?? [])
          .map(r => `${r.artifact}: ${r.error}`)
          .join(' | ');
        throw new Error(reasons || 'Queueing refused by the backend.');
      }
      setDialogOpen(false);
      setRows([
        {
          artifact: '',
          jira_ticket: '',
          build_run_url: '',
          prl1_only: false,
          df_only: false,
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

  const withdraw = useCallback(
    async (artifact: string) => {
      setError(null);
      try {
        await apiPost(apiBase, '/api/release-queue/withdraw', {
          artifact_name: artifact,
          requested_by: requestedBy.trim(),
        });
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [requestedBy, refresh, apiBase],
  );

  const items = ctx?.queue ?? [];

  return (
    <Card>
      <CardHeader
        title="Release intake queue"
        subheader="Next-release items; queued notes go to DevOps"
        action={
          <>
            <IconButton onClick={refresh} disabled={loading} size="small">
              <RefreshIcon />
            </IconButton>
            <Button
              size="small"
              variant="outlined"
              startIcon={<AddIcon />}
              onClick={() => setDialogOpen(true)}
            >
              Add items
            </Button>
          </>
        }
      />
      <CardContent>
        {loading && <Progress />}
        {error && <Typography color="error">{error}</Typography>}
        {!loading && !items.length && !error && (
          <Typography color="textSecondary">Queue is empty.</Typography>
        )}
        {items.length > 0 && (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Artifact</TableCell>
                <TableCell>Requested by</TableCell>
                <TableCell>Jira</TableCell>
                <TableCell>Flags</TableCell>
                <TableCell>Note</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((it, idx) => (
                <TableRow key={idx}>
                  <TableCell>
                    {it.artifact_name}:{it.artifact_version}
                  </TableCell>
                  <TableCell>{it.requested_by}</TableCell>
                  <TableCell>{it.jira_ticket}</TableCell>
                  <TableCell>
                    {[it.prl1_only ? 'PRL1' : '', it.df_only ? 'DF' : '']
                      .filter(Boolean)
                      .join(', ') || '—'}
                  </TableCell>
                  <TableCell>{it.note}</TableCell>
                  <TableCell align="right">
                    <Button
                      size="small"
                      color="secondary"
                      onClick={() =>
                        withdraw(`${it.artifact_name}:${it.artifact_version}`)
                      }
                    >
                      Withdraw
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {!ctx && !error && !loading && (
          <Typography color="textSecondary">
            Click refresh to load the queue.
          </Typography>
        )}
        {ctx?.error && !error && (
          <Typography color="error">{ctx.error}</Typography>
        )}

        <Dialog
          open={dialogOpen}
          onClose={() => setDialogOpen(false)}
          fullWidth
          maxWidth="md"
        >
          <DialogTitle>Add to next release</DialogTitle>
          <DialogContent>
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
              Charts
            </Typography>
            {rows.map((r, idx) => (
              <div
                key={idx}
                style={{
                  display: 'flex',
                  gap: 8,
                  alignItems: 'center',
                  marginBottom: 8,
                }}
              >
                <TextField
                  id={`queue-chart-${idx}`}
                  variant="outlined"
                  size="small"
                  label="chart:version"
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
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={r.prl1_only}
                      onChange={e =>
                        updateRow(idx, { prl1_only: e.target.checked })
                      }
                    />
                  }
                  label="PRL1 only"
                />
                <FormControlLabel
                  control={
                    <Checkbox
                      checked={r.df_only}
                      onChange={e =>
                        updateRow(idx, { df_only: e.target.checked })
                      }
                    />
                  }
                  label="DF image"
                />
                {rows.length > 1 && (
                  <Button size="small" onClick={() => removeRow(idx)}>
                    ✕
                  </Button>
                )}
              </div>
            ))}
            <Button size="small" onClick={addRow}>
              + Add another chart
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
