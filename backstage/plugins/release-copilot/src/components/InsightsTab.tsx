import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Chip,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from '@material-ui/core';
import { makeStyles } from '@material-ui/core/styles';
import {
  InfoCard,
  Link,
  Table,
  type TableColumn,
} from '@backstage/core-components';
import { apiGet, useApiBase } from '../api';

type ReleaseRow = {
  release: string;
  version: string;
  pr?: number;
  at: string;
};

type ChartInsight = {
  artifact_name: string;
  count: number;
  versions: string[];
  releases?: ReleaseRow[];
  last_at: string;
};

type InsightsResponse = {
  ok: boolean;
  total_events: number;
  chart_count: number;
  charts: ChartInsight[];
  days: number;
  event_type: string;
  error?: string;
};

/** One image currently deployed in one environment. */
type EnvImage = {
  artifact_name: string;
  version: string;
  since: string;
};

type EnvState = {
  environment: string;
  count: number;
  images: EnvImage[];
};

/**
 * `event_type=state` is not a window over events — it is the CURRENT estate,
 * derived by letting the latest deployed/removed event win per
 * (image, environment). So it deliberately ignores the history window.
 */
type StateResponse = {
  ok: boolean;
  environments: EnvState[];
  distinct_images: number;
  error?: string;
};

type Row = {
  chart: string;
  version: string;
  release: string;
  pr: number | null;
  at: string;
  count: number;
};

type EnvRow = {
  environment: string;
  image: string;
  tag: string;
  since: string;
};

const useStyles = makeStyles(theme => ({
  filters: {
    display: 'flex',
    gap: theme.spacing(2),
    marginBottom: theme.spacing(2),
    flexWrap: 'wrap',
    alignItems: 'center',
  },
  muted: { color: theme.palette.text.secondary },
  envChip: { marginRight: theme.spacing(1), marginBottom: theme.spacing(1) },
}));

/**
 * The promotion chain leads; anything else (dataflow-uat, ad-hoc envs) follows
 * alphabetically. Without this the estate reads in BigQuery's order, which puts
 * dataflow-uat above the environment a release actually promotes through.
 */
const ENV_ORDER = ['uat', 'prl1', 'prd'];

function envRank(env: string): number {
  const i = ENV_ORDER.indexOf(env.toLowerCase());
  return i === -1 ? ENV_ORDER.length : i;
}

function sortEnvs(envs: EnvState[]): EnvState[] {
  return [...envs].sort(
    (a, b) =>
      envRank(a.environment) - envRank(b.environment) ||
      a.environment.localeCompare(b.environment),
  );
}

function flattenEnvs(envs: EnvState[]): EnvRow[] {
  const rows: EnvRow[] = [];
  for (const e of sortEnvs(envs)) {
    for (const img of e.images) {
      rows.push({
        environment: e.environment,
        image: img.artifact_name,
        tag: img.version,
        since: img.since,
      });
    }
  }
  return rows;
}

function flatten(charts: ChartInsight[]): Row[] {
  const rows: Row[] = [];
  for (const c of charts) {
    for (const r of c.releases ?? []) {
      rows.push({
        chart: c.artifact_name,
        version: r.version,
        release: r.release,
        pr: r.pr ?? null,
        at: r.at,
        count: c.count,
      });
    }
  }
  rows.sort((a, b) => (a.at < b.at ? 1 : -1));
  return rows;
}

const columns: TableColumn<Row>[] = [
  { title: 'Chart', field: 'chart' },
  { title: 'Version', field: 'version' },
  { title: 'Release', field: 'release' },
  {
    title: 'PR',
    field: 'pr',
    render: (row: Row) =>
      row.pr === null || row.pr === undefined ? (
        '—'
      ) : (
        <Link to={`https://github.com/pull/${row.pr}`}>#{row.pr}</Link>
      ),
  },
  {
    title: 'When',
    field: 'at',
    render: (row: Row) => new Date(row.at).toLocaleString(),
  },
];

const envColumns: TableColumn<EnvRow>[] = [
  {
    title: 'Environment',
    field: 'environment',
    render: (row: EnvRow) => row.environment.toUpperCase(),
    customSort: (a: EnvRow, b: EnvRow) =>
      envRank(a.environment) - envRank(b.environment) ||
      a.environment.localeCompare(b.environment),
  },
  { title: 'Image', field: 'image' },
  { title: 'Tag', field: 'tag' },
  {
    title: 'Deployed since',
    field: 'since',
    render: (row: EnvRow) =>
      row.since ? new Date(row.since).toLocaleString() : '—',
  },
];

const DAY_OPTIONS = [
  { label: 'Last 7 days', value: 7 },
  { label: 'Last 30 days', value: 30 },
  { label: 'Last 90 days', value: 90 },
];

const ALL_ENVS = '__all__';

export function InsightsTab() {
  const classes = useStyles();
  const apiBase = useApiBase();
  const [data, setData] = useState<InsightsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState(7);
  const [pattern, setPattern] = useState('');
  const [eventType, setEventType] = useState<'released' | 'deployed'>(
    'released',
  );

  const [state, setState] = useState<StateResponse | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [stateLoading, setStateLoading] = useState(false);
  const [envFilter, setEnvFilter] = useState<string>(ALL_ENVS);

  // Debounced so typing "eod-risk-fetcher" costs one pair of BigQuery queries
  // instead of seventeen. Both cards read `debounced`, never `pattern`, so the
  // input stays responsive while the queries lag behind it.
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebounced(pattern.trim()), 350);
    return () => clearTimeout(t);
  }, [pattern]);

  // `pattern` is applied SERVER-side (glob or substring on the image name), so
  // the same box narrows both cards and the filtering matches what the agent
  // itself would answer in chat.
  const query = useMemo(
    () => (debounced ? `&pattern=${encodeURIComponent(debounced)}` : ''),
    [debounced],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiGet<InsightsResponse>(
        apiBase,
        `/api/release-insights?days=${days}&event_type=${eventType}${query}`,
      );
      if (result.ok === false) throw new Error(result.error || 'query failed');
      setData(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [apiBase, days, query, eventType]);

  const loadState = useCallback(async () => {
    setStateLoading(true);
    setStateError(null);
    try {
      const result = await apiGet<StateResponse>(
        apiBase,
        `/api/release-insights?event_type=state${query}`,
      );
      if (result.ok === false) throw new Error(result.error || 'query failed');
      setState(result);
    } catch (e) {
      setStateError((e as Error).message);
    } finally {
      setStateLoading(false);
    }
  }, [apiBase, query]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadState();
  }, [loadState]);

  const rows = data ? flatten(data.charts ?? []) : [];
  const envs = state ? sortEnvs(state.environments ?? []) : [];
  const envRows = flattenEnvs(state?.environments ?? []).filter(
    r => envFilter === ALL_ENVS || r.environment === envFilter,
  );

  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <InfoCard title="Currently deployed by environment">
          <div className={classes.filters}>
            <TextField
              variant="outlined"
              size="small"
              label="Image name (e.g. eod-risk or orders-*)"
              value={pattern}
              onChange={e => setPattern(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') setDebounced(pattern.trim());
              }}
              style={{ minWidth: 280 }}
              helperText="Filters both cards. Substring, or a glob with *"
            />
            <FormControl
              variant="outlined"
              size="small"
              style={{ minWidth: 180 }}
            >
              <InputLabel>Environment</InputLabel>
              <Select
                value={envFilter}
                label="Environment"
                onChange={e => setEnvFilter(String(e.target.value))}
              >
                <MenuItem value={ALL_ENVS}>All environments</MenuItem>
                {envs.map(e => (
                  <MenuItem key={e.environment} value={e.environment}>
                    {e.environment.toUpperCase()}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </div>
          {state && !stateError && (
            <>
              <div>
                {envs.map(e => (
                  <Chip
                    key={e.environment}
                    className={classes.envChip}
                    size="small"
                    color={envFilter === e.environment ? 'primary' : 'default'}
                    label={`${e.environment.toUpperCase()} · ${e.count}`}
                    onClick={() =>
                      setEnvFilter(
                        envFilter === e.environment ? ALL_ENVS : e.environment,
                      )
                    }
                  />
                ))}
              </div>
              <Typography
                variant="body2"
                className={classes.muted}
                gutterBottom
              >
                {state.distinct_images} distinct image
                {state.distinct_images === 1 ? '' : 's'} across {envs.length}{' '}
                environment{envs.length === 1 ? '' : 's'}. Current estate —
                latest deployed event wins per image per environment, so this is
                not limited by the history window below.
              </Typography>
            </>
          )}
          {stateError && <Typography color="error">{stateError}</Typography>}
          <Table<EnvRow>
            options={{ paging: false, search: false, padding: 'dense' }}
            isLoading={stateLoading}
            columns={envColumns}
            data={envRows}
            emptyContent={
              <Typography className={classes.muted} style={{ padding: 16 }}>
                {debounced
                  ? `No deployed images match "${debounced}".`
                  : 'No deployed images recorded yet — the estate is derived from deploy events in BigQuery.'}
              </Typography>
            }
          />
        </InfoCard>
      </Grid>
      <Grid item xs={12}>
        <InfoCard title="Release history">
          <div className={classes.filters}>
            <FormControl
              variant="outlined"
              size="small"
              style={{ minWidth: 160 }}
            >
              <InputLabel>Window</InputLabel>
              <Select
                value={days}
                label="Window"
                onChange={e => setDays(Number(e.target.value))}
              >
                {DAY_OPTIONS.map(o => (
                  <MenuItem key={o.value} value={o.value}>
                    {o.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl
              variant="outlined"
              size="small"
              style={{ minWidth: 160 }}
            >
              <InputLabel>Event</InputLabel>
              <Select
                value={eventType}
                label="Event"
                onChange={e =>
                  setEventType(e.target.value as 'released' | 'deployed')
                }
              >
                <MenuItem value="released">Released (PR merged)</MenuItem>
                <MenuItem value="deployed">Deployed (env)</MenuItem>
              </Select>
            </FormControl>
          </div>
          {data && !error && (
            <Typography variant="body2" className={classes.muted} gutterBottom>
              {data.total_events} {data.event_type} event
              {data.total_events === 1 ? '' : 's'} across {data.chart_count}{' '}
              chart{data.chart_count === 1 ? '' : 's'} in the last {data.days}{' '}
              days (BigQuery event log)
              {debounced ? ` matching "${debounced}"` : ''}.
            </Typography>
          )}
          {error && <Typography color="error">{error}</Typography>}
          <Table<Row>
            options={{ paging: false, search: false, padding: 'dense' }}
            isLoading={loading}
            columns={columns}
            data={rows}
            emptyContent={
              <Typography className={classes.muted} style={{ padding: 16 }}>
                No {eventType} events in this window — try a longer window.
              </Typography>
            }
          />
        </InfoCard>
      </Grid>
    </Grid>
  );
}
