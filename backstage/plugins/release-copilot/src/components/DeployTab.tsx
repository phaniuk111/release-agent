import { useCallback, useEffect, useState } from 'react';
import {
  Button,
  Card,
  CardContent,
  CardHeader,
  FormControl,
  Grid,
  InputLabel,
  makeStyles,
  MenuItem,
  Select,
  TextField,
  Typography,
} from '@material-ui/core';
import { apiGet, useApiBase } from '../api';

const useStyles = makeStyles(theme => ({
  jsonBox: {
    fontFamily: 'monospace',
    fontSize: '0.8rem',
  },
  sentNote: { color: theme.palette.success.main, marginTop: theme.spacing(1) },
}));

type DeployTemplate = {
  environment: string;
  deployment?: { include?: unknown[] };
  deploy_repo?: string;
  from_repo?: boolean;
};

type DeployPayload = {
  environment: string;
  include: unknown[];
  deployment_repo: string;
  change_request?: {
    chg_summary: string;
    description: string;
    start_date: string;
    end_date: string;
  };
};

export function DeployTab(props: {
  onSend: (text: string) => Promise<void>;
  /** Agent is streaming a response for the submitted payload. */
  busy?: boolean;
  /** Latest agent reply (preview / result) — rendered inline. */
  agentResponse?: string;
  /** Set when the agent's reply contains a CONFIRM token. */
  pendingConfirm?: string | null;
}) {
  const classes = useStyles();
  const {
    onSend,
    busy = false,
    agentResponse = '',
    pendingConfirm = null,
  } = props;
  const apiBase = useApiBase();
  const [env, setEnv] = useState<'uat' | 'prod'>('uat');
  const [json, setJson] = useState('');
  const [repo, setRepo] = useState('');
  const [summary, setSummary] = useState('');
  const [description, setDescription] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const isProd = env === 'prod';

  const loadTemplate = useCallback(
    async (target: 'uat' | 'prod') => {
      setError(null);
      setSent(false);
      try {
        const t = await apiGet<DeployTemplate>(
          apiBase,
          `/api/deploy-template?env=${target}`,
        );
        setJson(JSON.stringify(t.deployment ?? { include: [] }, null, 2));
        setRepo(t.deploy_repo ?? '');
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [apiBase],
  );

  useEffect(() => {
    loadTemplate(env);
  }, [env, loadTemplate]);

  const submit = useCallback(async () => {
    setError(null);
    setSent(false);
    let parsed: { include?: unknown[] };
    try {
      parsed = JSON.parse(json);
    } catch {
      setError('Deployment JSON is not valid JSON.');
      return;
    }
    const include = parsed.include ?? [];
    if (
      !include.length ||
      include.some(
        it =>
          !it ||
          typeof (it as { helm_chart_name?: string }).helm_chart_name !==
            'string' ||
          !(
            (it as { helm_chart_version?: string }).helm_chart_version ?? ''
          ).trim(),
      )
    ) {
      setError(
        'Each include entry needs a non-empty helm_chart_name + helm_chart_version.',
      );
      return;
    }
    if (!repo.trim()) {
      setError('Deployment repo is required (owner/repo).');
      return;
    }
    const payload: DeployPayload = {
      environment: env,
      include,
      deployment_repo: repo.trim(),
    };
    if (isProd) {
      if (!summary.trim() || !description.trim() || !start || !end) {
        setError(
          'PROD requires change summary, description, start time, and end time.',
        );
        return;
      }
      const s = new Date(start);
      const e2 = new Date(end);
      if (!(e2.getTime() > s.getTime())) {
        setError('Change end time must be after the start time.');
        return;
      }
      payload.change_request = {
        chg_summary: summary.trim(),
        description: description.trim(),
        start_date: s.toISOString(),
        end_date: e2.toISOString(),
      };
    }
    await onSend(JSON.stringify(payload));
    setSent(true);
  }, [json, env, repo, isProd, summary, description, start, end, onSend]);

  return (
    <Card>
      <CardHeader
        title="Deploy charts"
        subheader="Edits the live env deployment.json — submits through the chat preview → CONFIRM gate"
      />
      <CardContent>
        <Grid container spacing={2}>
          <Grid item xs={12} sm={4}>
            <FormControl fullWidth variant="outlined" size="small">
              <InputLabel>Environment</InputLabel>
              <Select
                id="deploy-env"
                value={env}
                label="Environment"
                onChange={e => setEnv(e.target.value as 'uat' | 'prod')}
              >
                <MenuItem value="uat">UAT</MenuItem>
                <MenuItem value="prod">PRD</MenuItem>
              </Select>
            </FormControl>
          </Grid>
          <Grid item xs={12} sm={8}>
            <TextField
              id="deploy-repo"
              fullWidth
              variant="outlined"
              size="small"
              label="Deployment repo (owner/repo)"
              value={repo}
              onChange={e => setRepo(e.target.value)}
            />
          </Grid>
          <Grid item xs={12}>
            <TextField
              id="deploy-json"
              fullWidth
              multiline
              minRows={8}
              variant="outlined"
              label={`deployment.${
                env === 'prod' ? 'prd' : 'uat'
              } override JSON`}
              value={json}
              onChange={e => setJson(e.target.value)}
              InputProps={{ className: classes.jsonBox }}
            />
          </Grid>
          {isProd && (
            <>
              <Grid item xs={12}>
                <TextField
                  id="deploy-chg-summary"
                  fullWidth
                  variant="outlined"
                  size="small"
                  label="Change summary"
                  value={summary}
                  onChange={e => setSummary(e.target.value)}
                />
              </Grid>
              <Grid item xs={12}>
                <TextField
                  id="deploy-chg-description"
                  fullWidth
                  multiline
                  minRows={2}
                  variant="outlined"
                  size="small"
                  label="Change description"
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  id="deploy-chg-start"
                  fullWidth
                  variant="outlined"
                  size="small"
                  type="datetime-local"
                  label="Change start"
                  InputLabelProps={{ shrink: true }}
                  value={start}
                  onChange={e => setStart(e.target.value)}
                />
              </Grid>
              <Grid item xs={12} sm={6}>
                <TextField
                  id="deploy-chg-end"
                  fullWidth
                  variant="outlined"
                  size="small"
                  type="datetime-local"
                  label="Change end"
                  InputLabelProps={{ shrink: true }}
                  value={end}
                  onChange={e => setEnd(e.target.value)}
                />
              </Grid>
            </>
          )}
        </Grid>
        {error && (
          <Typography color="error" style={{ marginTop: 8 }}>
            {error}
          </Typography>
        )}
        {sent && !busy && !pendingConfirm && (
          <Typography className={classes.sentNote}>
            Sent to the agent — the preview appears below when it responds. If
            the agent asks for confirmation, use the Confirm bar above.
          </Typography>
        )}
        <Button
          variant="contained"
          color="primary"
          style={{ marginTop: 12 }}
          onClick={submit}
          disabled={busy}
        >
          {busy ? 'Working…' : `Deploy to ${isProd ? 'PRD' : 'UAT'}`}
        </Button>
        {(busy || agentResponse) && (
          <>
            <Typography variant="subtitle2" style={{ marginTop: 16 }}>
              Agent response
            </Typography>
            <pre
              className={classes.jsonBox}
              style={{
                whiteSpace: 'pre-wrap',
                background: 'rgba(127,127,127,0.08)',
                padding: 12,
                borderRadius: 4,
                maxHeight: 320,
                overflowY: 'auto' as const,
                marginTop: 8,
              }}
            >
              {busy
                ? agentResponse || 'Preparing the deploy preview…'
                : agentResponse || 'No response yet.'}
              {busy && '▌'}
            </pre>
          </>
        )}
      </CardContent>
    </Card>
  );
}
