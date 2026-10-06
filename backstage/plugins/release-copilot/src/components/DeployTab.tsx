import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Grid,
  makeStyles,
  TextField,
  Typography,
} from '@material-ui/core';
import { apiGet, useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import { AgentMarkdown } from './AgentMarkdown';
import {
  cleanJira,
  JIRA_LABEL,
  JIRA_PLACEHOLDER,
  jiraError,
} from './jiraFormat';
import { deployIncludeProblem, parseDeployInclude } from './releaseFormat';
import { TurnResult } from './TurnResult';

const useStyles = makeStyles(theme => ({
  jsonBox: {
    fontFamily: 'monospace',
    fontSize: '0.8rem',
  },
  note: {
    marginBottom: theme.spacing(1.5),
    color: theme.palette.type === 'dark' ? P.amber : '#b45309',
  },
  // The portal's amber warning: this deploy would be refused right now.
  blocked: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: theme.spacing(1.5),
    marginBottom: theme.spacing(2),
    padding: theme.spacing(1, 1.5),
    borderRadius: P.radius.control,
    border: `1px solid ${P.amberBorder}`,
    background:
      theme.palette.type === 'dark'
        ? P.amberSurface
        : 'rgba(245, 158, 11, 0.08)',
  },
}));

type DeployTemplate = {
  ok?: boolean;
  error?: string;
  environment?: string;
  deployment?: { include?: unknown[] };
  deploy_repo?: string;
  from_repo?: boolean;
  /** The branch whose file this is — SIT, where UAT changes flow through. */
  branch?: string;
  /** The server's sentence when a UAT deploy would be refused right now; '' otherwise. */
  blocked?: string;
};

/** The editor's content when the live file could not be read. */
const BLANK = { include: [{ helm_chart_name: '', helm_chart_version: '' }] };

/**
 * Deploy to CARE UAT — the portal's deploy_form.js. The editor holds the WHOLE
 * current uat/deployment.json; submit OVERRIDES the file with exactly what it
 * shows, through the chat's preview → CONFIRM gate.
 *
 * UAT only: PROD is reached by promoting a release (Releases tab), never by
 * deploying one chart — the backend refuses a prod deploy as well.
 */
export function DeployTab(props: {
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
  const classes = useStyles();
  const {
    onSend,
    busy = false,
    result = { text: '', streaming: false, pendingToken: null },
    onConfirm = () => {},
    onCancel = () => {},
  } = props;
  const apiBase = useApiBase();
  const [json, setJson] = useState('');
  const [repo, setRepo] = useState('');
  const [jira, setJira] = useState('');
  const [branch, setBranch] = useState('');
  const [blocked, setBlocked] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const loadTemplate = useCallback(async () => {
    // An older, slower answer must not land over a newer one ("Check again").
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const t = await apiGet<DeployTemplate>(
        apiBase,
        '/api/deploy-template?env=uat',
      );
      if (seq !== loadSeq.current) return;
      if (t.ok === false)
        throw new Error(t.error || 'the template was refused');
      setJson(JSON.stringify(t.deployment ?? BLANK, null, 2));
      setRepo(prev => prev || t.deploy_repo || '');
      setBranch(t.branch ?? '');
      setBlocked(t.blocked ?? '');
      setLoadError(null);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setLoadError((e as Error).message);
      setJson(prev => prev || JSON.stringify(BLANK, null, 2));
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    void loadTemplate();
  }, [loadTemplate]);

  const submit = useCallback(async () => {
    setError(null);
    const parsed = parseDeployInclude(json);
    const problem = deployIncludeProblem(parsed);
    if (problem || !parsed) {
      setError(problem);
      return;
    }
    if (!repo.trim()) {
      setError('Deployment repo is required (owner/repo).');
      return;
    }
    if (jiraError(jira)) {
      setError(jiraError(jira));
      return;
    }
    const payload = {
      environment: 'uat',
      include: parsed.include,
      deployment_repo: repo.trim(),
      jira: cleanJira(jira),
    };
    // Show exactly what was parsed (commas added, wrapped into include[]) — the
    // file is overwritten with this and nothing else.
    setJson(JSON.stringify({ include: parsed.include }, null, 2));
    await onSend(JSON.stringify(payload));
  }, [json, repo, jira, onSend]);

  return (
    <Card>
      <CardHeader
        title="Deploy to CARE UAT"
        subheader={`— current uat/deployment.json${
          branch ? ` on ${branch}` : ''
        }; edit (add/remove entries), then submit OVERRIDES the file with exactly what you see`}
      />
      <CardContent>
        {loadError && (
          <Typography
            variant="body2"
            className={classes.note}
            data-testid="deploy-load-note"
          >
            Couldn't load the live deployment.json ({loadError}) — the form
            still works; fields aren't pre-filled.
          </Typography>
        )}
        {blocked && (
          <Box className={classes.blocked} data-testid="deploy-blocked">
            <span aria-hidden>⚠</span>
            <Box flex={1}>
              <AgentMarkdown text={blocked} />
            </Box>
            <Button
              size="small"
              variant="outlined"
              onClick={() => void loadTemplate()}
              disabled={loading}
            >
              Check again
            </Button>
          </Box>
        )}
        <Grid container spacing={2}>
          <Grid item xs={12}>
            <TextField
              id="deploy-json"
              fullWidth
              multiline
              minRows={12}
              variant="outlined"
              label="uat/deployment.json"
              value={json}
              onChange={e => setJson(e.target.value)}
              InputProps={{ className: classes.jsonBox }}
              inputProps={{ spellCheck: false }}
            />
          </Grid>
          <Grid item xs={12} sm={6}>
            <TextField
              id="deploy-repo"
              fullWidth
              variant="outlined"
              size="small"
              label="Deployment repo (owner/repo)"
              placeholder="e.g. my-org/deployment-repo"
              value={repo}
              onChange={e => setRepo(e.target.value)}
            />
          </Grid>
          <Grid item xs={12} sm={6}>
            <TextField
              id="deploy-jira"
              fullWidth
              variant="outlined"
              size="small"
              label={JIRA_LABEL}
              placeholder={JIRA_PLACEHOLDER}
              value={jira}
              onChange={e => setJira(e.target.value)}
            />
          </Grid>
        </Grid>
        {error && (
          <Typography
            color="error"
            style={{ marginTop: 8 }}
            data-testid="deploy-error"
          >
            {error}
          </Typography>
        )}
        <Button
          variant="contained"
          color="primary"
          style={{ marginTop: 12 }}
          onClick={submit}
          // Refused anyway while blocked (at preview and again at confirm) —
          // said before anyone fills the form in, not after.
          disabled={busy || loading || !!blocked}
        >
          {busy ? 'Working…' : 'Deploy to CARE UAT'}
        </Button>
        <TurnResult {...result} onConfirm={onConfirm} onCancel={onCancel} />
      </CardContent>
    </Card>
  );
}
