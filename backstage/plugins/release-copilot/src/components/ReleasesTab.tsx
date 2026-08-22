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
  queue?: Array<{ artifact_name?: string; artifact_version?: string }>;
  default_repo?: string;
  df_default_repo?: string;
};

export function ReleasesTab() {
  const apiBase = useApiBase();
  const [kind, setKind] = useState<'care' | 'df'>('care');
  const [ctx, setCtx] = useState<QueueCtx | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [draft, setDraft] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    apiGet<QueueCtx>(apiBase, '/api/release-queue')
      .then(setCtx)
      .catch(e => setError((e as Error).message));
  }, [apiBase]);

  const artifacts = (ctx?.queue ?? [])
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
    const artifactsPicked = Object.keys(selected).filter(a => selected[a]);
    if (!artifactsPicked.length) {
      setError('Tick at least one queued item.');
      return;
    }
    setLoading(true);
    try {
      const result = await apiPost(apiBase, '/api/release-draft', {
        artifacts: artifactsPicked,
        kind,
      });
      setDraft(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [selected, kind, apiBase]);

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
            onChange={e => setKind(e.target.value as 'care' | 'df')}
          >
            <MenuItem value="care">CARE release</MenuItem>
            <MenuItem value="df">Dataflow release</MenuItem>
          </Select>
        </FormControl>
        {artifacts.length === 0 && (
          <Typography color="textSecondary">
            No queued items — add charts on the Queue tab first.
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
        {draft != null && (
          <pre
            style={{
              marginTop: 12,
              overflowX: 'auto',
              fontSize: '0.8rem',
              whiteSpace: 'pre-wrap',
            }}
          >
            {JSON.stringify(draft, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
