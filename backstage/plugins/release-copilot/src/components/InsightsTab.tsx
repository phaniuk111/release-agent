import { useCallback, useEffect, useState } from 'react';
import {
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

type Row = {
  chart: string;
  version: string;
  release: string;
  pr: number | null;
  at: string;
  count: number;
};

const useStyles = makeStyles(theme => ({
  filters: { display: 'flex', gap: theme.spacing(2), marginBottom: theme.spacing(2) },
  muted: { color: theme.palette.text.secondary },
}));

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
      row.pr == null ? (
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

const DAY_OPTIONS = [
  { label: 'Last 7 days', value: 7 },
  { label: 'Last 30 days', value: 30 },
  { label: 'Last 90 days', value: 90 },
];

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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams({
        days: String(days),
        event_type: eventType,
        ...(pattern.trim() ? { pattern: pattern.trim() } : {}),
      });
      const result = await apiGet<InsightsResponse>(
        apiBase,
        `/api/release-insights?${qs}`,
      );
      if (result.ok === false) throw new Error(result.error || 'query failed');
      setData(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [apiBase, days, pattern, eventType]);

  useEffect(() => {
    load();
  }, [load]);

  const rows = data ? flatten(data.charts ?? []) : [];

  return (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <InfoCard title="Release history">
          <div className={classes.filters}>
            <FormControl variant="outlined" size="small" style={{ minWidth: 160 }}>
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
            <FormControl variant="outlined" size="small" style={{ minWidth: 160 }}>
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
            <TextField
              variant="outlined"
              size="small"
              label="Chart pattern (e.g. orders-*)"
              value={pattern}
              onChange={e => setPattern(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') load();
              }}
              style={{ minWidth: 240 }}
            />
          </div>
          {data && !error && (
            <Typography variant="body2" className={classes.muted} gutterBottom>
              {data.total_events} {data.event_type} event
              {data.total_events === 1 ? '' : 's'} across {data.chart_count}{' '}
              chart{data.chart_count === 1 ? '' : 's'} in the last {data.days}{' '}
              days (BigQuery event log).
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
