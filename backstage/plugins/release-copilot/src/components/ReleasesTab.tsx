import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  Checkbox,
  FormControl,
  FormControlLabel,
  FormGroup,
  InputLabel,
  MenuItem,
  Select,
  Typography,
} from '@material-ui/core';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';

type QueueCtx = {
  queue?: Array<{ artifact_name?: string; artifact_version?: string; df_only?: boolean }>;
  default_repo?: string;
  df_default_repo?: string;
};

type DraftResponse = { ok?: boolean; error?: string; draft?: Record<string, string> };

// The change-request fields, in the order a CAB form asks for them.
const FIELD_LABELS: Record<string, string> = {
  change_summary: 'Change summary',
  change_description: 'Change description',
  change_reason: 'Reason for change',
  associated_risk: 'Associated risk',
  consequence: 'Consequence of not doing it',
  user_service_impact: 'User / service impact',
};
const label = (key: string) =>
  FIELD_LABELS[key] ?? key.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());

export function ReleasesTab() {
  const apiBase = useApiBase();
  const [kind, setKind] = useState<'care' | 'df'>('care');
  const [ctx, setCtx] = useState<QueueCtx | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    apiGet<QueueCtx>(apiBase, '/api/release-queue')
      .then(setCtx)
      .catch(e => setError((e as Error).message));
  }, [apiBase]);

  // A release carries only its own kind: a DF release never picks up CARE
  // charts, and the reverse (the release forms filter the same way).
  const ofKind = (ctx?.queue ?? []).filter(r => (kind === 'df' ? !!r.df_only : !r.df_only));
  const otherKind = (ctx?.queue?.length ?? 0) - ofKind.length;
  const artifacts = ofKind
    .map(r =>
      r.artifact_name ? `${r.artifact_name}:${r.artifact_version ?? ''}` : null,
    )
    .filter((a): a is string => !!a);

  const toggle = useCallback((artifact: string) => {
    setSelected(prev => ({ ...prev, [artifact]: !prev[artifact] }));
  }, []);

  const submit = useCallback(async () => {
    setError(null);
    setDraft(null);
    // Only ticks on items of THIS kind — a tick left from the other kind is not visible.
    const artifactsPicked = artifacts.filter(a => selected[a]);
    if (!artifactsPicked.length) {
      setError('Tick at least one queued item.');
      return;
    }
    setLoading(true);
    try {
      const result = await apiPost<DraftResponse>(apiBase, '/api/release-draft', {
        artifacts: artifactsPicked,
        kind,
      });
      setDraft(result.draft ?? {});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selected, kind, apiBase, artifacts]);

  return (
    <Card>
      <CardHeader
        title="Draft a release"
        subheader="Drafts change-request prose from queued items — drafting only, the release still runs the preview → CONFIRM path"
      />
      <CardContent>
        <FormControl
          variant="outlined"
          size="small"
          style={{ minWidth: 200, marginBottom: 12 }}
        >
          <InputLabel>Release kind</InputLabel>
          <Select
            value={kind}
            label="Release kind"
            onChange={e => {
              setKind(e.target.value as 'care' | 'df');
              setSelected({});
              setDraft(null);
            }}
          >
            <MenuItem value="care">CARE release</MenuItem>
            <MenuItem value="df">Dataflow release</MenuItem>
          </Select>
        </FormControl>
        {artifacts.length === 0 && (
          <Typography color="textSecondary">
            Nothing is queued for the {kind === 'df' ? 'Dataflow' : 'CARE'} release
            {otherKind > 0
              ? ` — ${otherKind} item${otherKind === 1 ? ' is' : 's are'} queued for the ${
                  kind === 'df' ? 'CARE' : 'Dataflow'
                } release.`
              : ' — add charts on the Queue tab first.'}
          </Typography>
        )}
        <FormGroup>
          {artifacts.map(a => (
            <FormControlLabel
              key={a}
              control={
                <Checkbox checked={!!selected[a]} onChange={() => toggle(a)} />
              }
              label={a}
            />
          ))}
        </FormGroup>
        {error && (
          <Typography color="error" style={{ marginTop: 8 }}>
            {error}
          </Typography>
        )}
        {loading && <Progress />}
        <Button
          variant="contained"
          color="primary"
          style={{ marginTop: 12 }}
          onClick={submit}
          disabled={loading || !artifacts.length}
        >
          Draft change request
        </Button>
        {draft !== null && (
          <div style={{ marginTop: 16 }}>
            {Object.entries(draft)
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <div key={k} style={{ marginBottom: 12 }}>
                  <Typography variant="caption" color="textSecondary" display="block">
                    {label(k)}
                  </Typography>
                  <Typography variant="body2" style={{ whiteSpace: 'pre-wrap' }}>
                    {v}
                  </Typography>
                </div>
              ))}
            <Typography variant="caption" color="textSecondary">
              A draft to copy into the change request — nothing was submitted.
            </Typography>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
