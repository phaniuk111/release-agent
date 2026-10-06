import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MonitoringTab } from './MonitoringTab';

const BASE = 'http://backend/api/proxy/release-copilot';

jest.mock('../api', () => ({
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
}));

const REPORT = {
  ok: true,
  project: 'example-project',
  region: 'region-us',
  days: 14,
  billing: 'on-demand',
  scanned_at: '2026-09-19T10:00:00Z',
  report_cost_bytes: 1024,
  totals: { queries: 1240, slot_hours: 612, gb_billed: 84.2 },
  shapes: [
    {
      qhash: 'small1',
      runs: 3,
      who: 'bob@example.com',
      gb_billed: 1,
      approx_usd: 0.01,
      p50_bytes: 21557,
      sql_preview: 'SELECT a FROM dataset_a.small',
    },
    {
      qhash: 'abc123',
      runs: 96,
      who: 'alice@example.com',
      users: ['alice@example.com', 'bob@example.com'],
      users_count: 3,
      gb_billed: 18432,
      approx_usd: 112.5,
      p50_gb: 192,
      insights: ['slot_contention'],
      sql_preview: 'SELECT * FROM dataset_a.events',
    },
  ],
  storage: [{}],
  writes: [],
};

const CHECKS = {
  source: 'Managed Service for Prometheus · example-project',
  checked_at: '2026-10-06 09:00:00 UTC',
  checks: [
    {
      name: 'Healthy thing',
      state: 'ok',
      query: 'up',
      watching: 17,
      series: [],
    },
    {
      name: 'Targets down',
      state: 'firing',
      query: 'up == 0',
      count: 3,
      series: [{ labels: { __name__: 'up', job: 'api' }, value: 0 }],
    },
    { name: 'Elsewhere', state: 'no_data', query: 'foo', series: [] },
  ],
};

function respond(routes: Record<string, { status?: number; body: unknown }>) {
  global.fetch = jest.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const path = url.slice(BASE.length).split('?')[0];
    const hit = routes[path];
    if (!hit) throw new Error(`unexpected fetch ${url}`);
    return {
      ok: (hit.status ?? 200) < 400,
      status: hit.status ?? 200,
      json: async () => hit.body,
    } as Response;
  }) as jest.Mock;
}

const idle = { text: '', streaming: false, pendingToken: null };

describe('MonitoringTab', () => {
  it('renders both sections, the costliest shape first, with the Excel link', async () => {
    respond({
      '/api/bq-cost/report': { body: REPORT },
      '/api/monitoring': { body: CHECKS },
    });
    render(<MonitoringTab onSend={jest.fn()} busy={false} result={idle} llm />);

    expect(
      await screen.findByText(
        '1,240 queries · 612 slot-hours · 84.2 GB billed · this report read 1.0 KB',
      ),
    ).toBeInTheDocument();
    const rows = screen.getAllByTestId('bq-shape');
    expect(rows[0]).toHaveTextContent('alice +2');
    expect(rows[0]).toHaveTextContent('18.0 TB');
    expect(rows[0]).toHaveTextContent('$112.50');
    expect(rows[0]).toHaveTextContent('slot contention');
    expect(rows[1]).toHaveTextContent('21.1 KB');
    expect(
      screen.getByText(/storage: 1 finding — in the Excel/),
    ).toBeInTheDocument();
    expect(screen.getByText('Download Excel').closest('a')).toHaveAttribute(
      'href',
      `${BASE}/api/bq-cost/report.xlsx`,
    );

    expect(
      await screen.findByText('1 firing · 1 OK · 1 not measured here'),
    ).toBeInTheDocument();
    const checks = screen.getAllByTestId('monitor-check');
    expect(checks[0]).toHaveTextContent('Targets down');
    expect(checks[0]).toHaveTextContent('firing (3)');
    expect(checks[0]).toHaveTextContent('job=api');
    expect(checks[1]).toHaveTextContent('watching 17');
    expect(
      screen.getByText(
        '1 not measured here — no data for them in this project',
      ),
    ).toBeInTheDocument();
  });

  it('hides Ask why without a model', async () => {
    respond({
      '/api/bq-cost/report': { body: REPORT },
      '/api/monitoring': { body: CHECKS },
    });
    render(
      <MonitoringTab
        onSend={jest.fn()}
        busy={false}
        result={idle}
        llm={false}
      />,
    );
    await screen.findAllByTestId('bq-shape');
    await screen.findAllByTestId('monitor-check');
    expect(screen.queryByText('Ask why')).toBeNull();
  });

  it("asks the chat the portal's question and shows the reply under the table", async () => {
    respond({
      '/api/bq-cost/report': { body: REPORT },
      '/api/monitoring': { body: CHECKS },
    });
    const onSend = jest.fn();
    const { rerender } = render(
      <MonitoringTab onSend={onSend} busy={false} result={idle} llm />,
    );
    const rows = await screen.findAllByTestId('bq-shape');
    fireEvent.click(rows[0].querySelector('button')!);
    expect(onSend).toHaveBeenCalledWith(
      'Why is BigQuery query shape abc123 expensive (96 runs, 18.0 TB billed)? Use bq_query_detail, ' +
        'look at the referenced tables, and propose a cheaper rewrite tested with bq_verify_rewrite.',
    );
    rerender(
      <MonitoringTab
        onSend={onSend}
        busy={false}
        result={{
          text: 'Partition the events table.',
          streaming: false,
          pendingToken: null,
        }}
        llm
      />,
    );
    expect(screen.getByText('Partition the events table.')).toBeInTheDocument();

    const checks = await screen.findAllByTestId('monitor-check');
    fireEvent.click(checks[0].querySelector('button')!);
    expect(onSend).toHaveBeenLastCalledWith(
      'The monitoring check "Targets down" is firing (PromQL: up == 0). Show me what it found and help me work out why.',
    );
  });

  it('says why a report is unavailable, and hints how to enable a disabled one', async () => {
    respond({
      '/api/bq-cost/report': {
        status: 403,
        body: { ok: false, error: 'not for you yet' },
      },
      '/api/monitoring': { status: 500, body: { ok: false, error: 'boom' } },
    });
    render(<MonitoringTab onSend={jest.fn()} busy={false} result={idle} llm />);
    expect(
      await screen.findByText('BigQuery cost report is unavailable'),
    ).toBeInTheDocument();
    expect(screen.getByText('not for you yet')).toBeInTheDocument();
    expect(screen.queryByText('Download Excel')).toBeNull();
    expect(
      await screen.findByText('Monitoring is unavailable: boom'),
    ).toBeInTheDocument();
  });

  it('a disabled report shows the enable hint; a partial one lists the roles', async () => {
    respond({
      '/api/bq-cost/report': {
        body: { ok: false, disabled: true, error: 'disabled' },
      },
      '/api/monitoring': { body: { checks: [] } },
    });
    const { unmount } = render(
      <MonitoringTab onSend={jest.fn()} busy={false} result={idle} llm />,
    );
    expect(
      await screen.findByText(/To enable it, set BQ_COST_REGION/),
    ).toBeInTheDocument();
    expect(await screen.findByText('no checks configured')).toBeInTheDocument();
    unmount();

    respond({
      '/api/bq-cost/report': {
        body: {
          ok: true,
          days: 14,
          shapes: [],
          shapes_error: '403',
          hint: 'only your own jobs are visible',
          missing_access: [
            {
              role: 'roles/bigquery.resourceViewer',
              grant_on: 'project example-project',
              permission: 'bigquery.jobs.listAll',
              sections: ['Query costs'],
            },
          ],
        },
      },
      '/api/monitoring': { body: { checks: [] } },
    });
    render(<MonitoringTab onSend={jest.fn()} busy={false} result={idle} llm />);
    expect(await screen.findByTestId('bq-access')).toHaveTextContent(
      'roles/bigquery.resourceViewer',
    );
    expect(screen.getByTestId('bq-access')).toHaveTextContent(
      'unlocks Query costs',
    );
    expect(
      screen.getByText('only your own jobs are visible'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'query costs unavailable — this account needs more access (see above)',
      ),
    ).toBeInTheDocument();
  });

  it('builds the alert policy for a check on demand', async () => {
    respond({
      '/api/bq-cost/report': { body: REPORT },
      '/api/monitoring': { body: CHECKS },
      '/api/monitoring/alert-policy': {
        body: { ok: true, policy: { displayName: 'Targets down' } },
      },
    });
    render(<MonitoringTab onSend={jest.fn()} busy={false} result={idle} llm />);
    const checks = await screen.findAllByTestId('monitor-check');
    fireEvent.click(screen.getAllByText('Make it an alert')[0]);
    await waitFor(() =>
      expect(checks[0]).toHaveTextContent('"displayName": "Targets down"'),
    );
    expect(checks[0]).toHaveTextContent(
      'gcloud alpha monitoring policies create --policy-from-file=policy.json',
    );
  });
});
