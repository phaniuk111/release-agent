import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  TextField,
  Typography,
} from '@material-ui/core';
import { apiGet, useApiBase } from '../api';
import { TurnResult } from './TurnResult';

type DfTemplate = {
  deploy_repo?: string;
  composer_repo?: string;
  composer_dir?: string;
  [key: string]: unknown;
};

export function DataflowTab(props: {
  onSend: (text: string) => Promise<void>;
  /** Any turn is in flight (one at a time, across tabs). */
  busy?: boolean;
  /** The reply to THIS tab's latest submission — preview, token, outcome. */
  result?: { text: string; streaming: boolean; pendingToken: string | null };
  onConfirm?: () => void;
  onCancel?: () => void;
}) {
  const {
    onSend,
    busy = false,
    result = { text: '', streaming: false, pendingToken: null },
    onConfirm = () => {},
    onCancel = () => {},
  } = props;
  const apiBase = useApiBase();
  const [image, setImage] = useState('');
  const [tag, setTag] = useState('');
  const [dags, setDags] = useState('');
  const [composerRepo, setComposerRepo] = useState('');
  const [deployRepo, setDeployRepo] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const loadDefaults = useCallback(async () => {
    setError(null);
    try {
      const t = await apiGet<DfTemplate>(apiBase, '/api/df-template?env=uat');
      if (t.composer_repo) setComposerRepo(t.composer_repo);
      if (t.deploy_repo) setDeployRepo(t.deploy_repo);
      setLoaded(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [apiBase]);

  useEffect(() => {
    if (!loaded) void loadDefaults();
  }, [loaded, loadDefaults]);

  const submit = useCallback(async () => {
    setError(null);
    if (!image.trim() || !tag.trim()) {
      setError('Image name and tag are both required.');
      return;
    }
    const payload: Record<string, unknown> = {
      deployment_type: 'dataflow',
      environment: 'uat',
      image: image.trim(),
      tag: tag.trim(),
    };
    const dagList = dags
      .split('\n')
      .map(l => l.trim())
      .filter(Boolean);
    if (dagList.length) {
      if (!composerRepo.trim()) {
        setError(
          'Name the Composer DAGs repo (owner/repo) for those DAG files.',
        );
        return;
      }
      payload.dag_files = dagList;
      payload.composer_repo = composerRepo.trim();
    }
    if (deployRepo.trim()) payload.deployment_repo = deployRepo.trim();
    await onSend(JSON.stringify(payload));
  }, [image, tag, dags, composerRepo, deployRepo, onSend]);

  return (
    <Card>
      <CardHeader
        title="Deploy Dataflow image"
        subheader="DF UAT deploy — a PR is raised, nothing is merged for you"
      />
      <CardContent>
        <TextField
          fullWidth
          variant="outlined"
          size="small"
          label="Image name"
          style={{ marginBottom: 12 }}
          value={image}
          onChange={e => setImage(e.target.value)}
        />
        <TextField
          fullWidth
          variant="outlined"
          size="small"
          label="Tag"
          style={{ marginBottom: 12 }}
          value={tag}
          onChange={e => setTag(e.target.value)}
        />
        <TextField
          fullWidth
          multiline
          minRows={3}
          variant="outlined"
          size="small"
          label="Composer DAG files (one per line, optional)"
          style={{ marginBottom: 12 }}
          value={dags}
          onChange={e => setDags(e.target.value)}
        />
        <TextField
          fullWidth
          variant="outlined"
          size="small"
          label="Composer DAGs repo (owner/repo)"
          style={{ marginBottom: 12 }}
          value={composerRepo}
          onChange={e => setComposerRepo(e.target.value)}
        />
        <TextField
          fullWidth
          variant="outlined"
          size="small"
          label="Deploy repo override (owner/repo, optional)"
          value={deployRepo}
          onChange={e => setDeployRepo(e.target.value)}
        />
        {error && (
          <Typography color="error" style={{ marginTop: 8 }}>
            {error}
          </Typography>
        )}
        <Button
          variant="contained"
          color="primary"
          style={{ marginTop: 12 }}
          onClick={submit}
          disabled={busy}
        >
          {busy ? 'Working…' : 'Deploy to DF UAT'}
        </Button>
        <TurnResult
          {...result}
          onConfirm={onConfirm}
          onCancel={onCancel}
          confirmLabel="Confirm & dispatch"
        />
      </CardContent>
    </Card>
  );
}
