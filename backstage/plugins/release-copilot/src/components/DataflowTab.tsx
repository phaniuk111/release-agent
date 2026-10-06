import { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Collapse,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from '@material-ui/core';
import { apiGet, useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import {
  cleanJira,
  JIRA_LABEL,
  JIRA_PLACEHOLDER,
  jiraError,
} from './jiraFormat';
import { TurnResult } from './TurnResult';

/** One dispatch input, described from the target workflow's own `inputs:`. */
type FieldSpec = {
  name?: string;
  label?: string;
  options?: string[];
  default?: string;
  description?: string;
};

type DfTemplate = {
  deploy_repo?: string;
  workflow?: string;
  fields?: { image?: FieldSpec; tag?: FieldSpec };
  composer_repo?: string;
  composer_dir?: string;
};

const IMAGE_FALLBACK: FieldSpec = {
  name: 'image',
  label: 'Image name',
  options: [],
};
const TAG_FALLBACK: FieldSpec = { name: 'tag', label: 'Tag', options: [] };

/**
 * A dispatch input as the form shows it: labelled with the WORKFLOW's own input
 * name, and a dropdown for a `choice` input — GitHub rejects any value outside
 * its options:, so free text there only earns a refusal after confirming.
 */
function DispatchField(props: {
  id: string;
  spec: FieldSpec;
  fallbackLabel: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const { id, spec, fallbackLabel, placeholder, value, onChange } = props;
  const label = spec.label || fallbackLabel;
  const options = spec.options ?? [];
  if (options.length) {
    return (
      <FormControl
        fullWidth
        variant="outlined"
        size="small"
        title={spec.description}
      >
        <InputLabel id={`${id}-label`}>{label}</InputLabel>
        <Select
          id={id}
          labelId={`${id}-label`}
          label={label}
          value={value}
          onChange={e => onChange(String(e.target.value))}
        >
          <MenuItem value="">
            <em>select {label.toLowerCase()}…</em>
          </MenuItem>
          {options.map(o => (
            <MenuItem key={o} value={o}>
              {o}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
    );
  }
  return (
    <TextField
      id={id}
      fullWidth
      variant="outlined"
      size="small"
      label={label}
      placeholder={placeholder}
      title={spec.description}
      value={value}
      onChange={e => onChange(e.target.value)}
    />
  );
}

/** The value a field opens on: the workflow's default — for a choice, only if it is one of the options. */
function initialValue(spec: FieldSpec): string {
  const opts = spec.options ?? [];
  if (spec.default === undefined || spec.default === null) return '';
  if (opts.length && !opts.includes(spec.default)) return '';
  return spec.default;
}

/**
 * Deploy to DF UAT — the portal's df_deploy_form.js. Dispatches the DF repo's
 * deploy workflow (and, when DAG files are named, raises a Composer PR bumping
 * their template version — nothing is merged for you). Same preview → CONFIRM
 * contract as every other deploy.
 */
export function DataflowTab(props: {
  onSend: (text: string) => Promise<void>;
  /** Any turn is in flight (one at a time, across tabs). */
  busy?: boolean;
  /** The reply to THIS tab's latest submission — preview, token, outcome. */
  result?: {
    text: string;
    streaming: boolean;
    pendingToken: string | null;
    progress?: string[];
  };
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
  const [ctx, setCtx] = useState<DfTemplate>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [image, setImage] = useState('');
  const [tag, setTag] = useState('');
  const [jira, setJira] = useState('');
  const [dags, setDags] = useState('');
  const [composerRepo, setComposerRepo] = useState('');
  const [deployRepo, setDeployRepo] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    apiGet<DfTemplate>(apiBase, '/api/df-template?env=uat')
      .then(t => {
        if (!live) return;
        setCtx(t);
        // Only fill what nobody has typed into yet.
        setImage(
          prev => prev || initialValue(t.fields?.image ?? IMAGE_FALLBACK),
        );
        setTag(prev => prev || initialValue(t.fields?.tag ?? TAG_FALLBACK));
        setComposerRepo(prev => prev || t.composer_repo || '');
        setDeployRepo(prev => prev || t.deploy_repo || '');
      })
      .catch(e => {
        if (live) setLoadError((e as Error).message);
      });
    return () => {
      live = false;
    };
  }, [apiBase]);

  const fImage = ctx.fields?.image ?? IMAGE_FALLBACK;
  const fTag = ctx.fields?.tag ?? TAG_FALLBACK;
  const workflow = ctx.workflow || 'df-deploy.yml';
  const i = image.trim();
  const t = tag.trim();
  // The real dispatch inputs, as the preview will show them.
  const echo =
    i && t
      ? `↳ will dispatch ${fImage.name}=${i} ${fTag.name}=${t} → uat ✓`
      : '';

  const submit = useCallback(async () => {
    setError(null);
    if (!i || !t) {
      setError(
        `${fImage.label || 'Image name'} and ${(
          fTag.label || 'tag'
        ).toLowerCase()} are both required.`,
      );
      return;
    }
    if (jiraError(jira)) {
      setError(jiraError(jira));
      return;
    }
    const payload: Record<string, unknown> = {
      deployment_type: 'dataflow',
      environment: 'uat',
      image: i,
      tag: t,
      jira: cleanJira(jira),
    };
    const dagList = dags
      .split('\n')
      .map(l => l.trim())
      .filter(Boolean);
    if (dagList.length) {
      payload.dag_files = dagList;
      if (!composerRepo.trim()) {
        setError(
          'Name the Composer DAGs repo (owner/repo) for those DAG files.',
        );
        return;
      }
      payload.composer_repo = composerRepo.trim();
    }
    if (deployRepo.trim()) payload.deployment_repo = deployRepo.trim();
    await onSend(JSON.stringify(payload));
  }, [
    i,
    t,
    fImage.label,
    fTag.label,
    jira,
    dags,
    composerRepo,
    deployRepo,
    onSend,
  ]);

  return (
    <Card>
      <CardHeader
        title="Deploy to DF UAT"
        subheader={`Triggers the ${workflow} workflow. Nothing runs until you confirm the preview.`}
      />
      <CardContent>
        {loadError && (
          <Typography
            variant="body2"
            style={{ marginBottom: 12, color: P.amber }}
          >
            Couldn't load recent DF runs ({loadError}) — the form still works;
            fields aren't pre-filled.
          </Typography>
        )}
        <Grid container spacing={2}>
          <Grid item xs={12} sm={6}>
            <DispatchField
              id="df-image"
              spec={fImage}
              fallbackLabel="Image name"
              placeholder="e.g. order-enrichment"
              value={image}
              onChange={setImage}
            />
          </Grid>
          <Grid item xs={12} sm={6}>
            <DispatchField
              id="df-tag"
              spec={fTag}
              fallbackLabel="Tag"
              placeholder="e.g. 1.4.2"
              value={tag}
              onChange={setTag}
            />
          </Grid>
        </Grid>
        <Typography
          variant="caption"
          display="block"
          data-testid="df-echo"
          style={{ minHeight: 18, margin: '6px 0 10px', color: P.emerald }}
        >
          {echo}
        </Typography>
        <TextField
          id="df-jira"
          fullWidth
          variant="outlined"
          size="small"
          label={JIRA_LABEL}
          placeholder={JIRA_PLACEHOLDER}
          style={{ marginBottom: 12 }}
          value={jira}
          onChange={e => setJira(e.target.value)}
        />
        <TextField
          id="df-dags"
          fullWidth
          multiline
          minRows={3}
          variant="outlined"
          size="small"
          label="Composer DAG file(s) (optional) — one .py per line; their default template version is bumped to this tag via a PR"
          placeholder={
            ctx.composer_dir
              ? `${ctx.composer_dir}/…  e.g.\nacme-svc-alpha.py\nacme-svc-beta.py`
              : 'acme-svc-alpha.py'
          }
          InputProps={{ style: { fontFamily: P.mono } }}
          inputProps={{ spellCheck: false }}
          style={{ marginBottom: 12 }}
          value={dags}
          onChange={e => setDags(e.target.value)}
        />
        <TextField
          id="df-composer-repo"
          fullWidth
          variant="outlined"
          size="small"
          label="Composer DAGs repo (owner/repo)"
          placeholder="e.g. my-org/composer-dags"
          value={composerRepo}
          onChange={e => setComposerRepo(e.target.value)}
        />
        <Typography
          variant="caption"
          color="textSecondary"
          display="block"
          style={{ margin: '4px 0 8px' }}
        >
          Files are read from {ctx.composer_dir || '<env>'}/ on the DAG branch.
          A PR is raised — nothing is merged for you.
        </Typography>
        <Box>
          <Button size="small" onClick={() => setAdvanced(a => !a)}>
            {advanced ? '▾' : '▸'} Advanced — repo override
          </Button>
          <Collapse in={advanced}>
            <TextField
              id="df-repo"
              fullWidth
              variant="outlined"
              size="small"
              label="Deploy repo override (owner/repo)"
              style={{ marginTop: 8 }}
              value={deployRepo}
              onChange={e => setDeployRepo(e.target.value)}
            />
          </Collapse>
        </Box>
        {error && (
          <Typography
            color="error"
            style={{ marginTop: 8 }}
            data-testid="df-error"
          >
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
