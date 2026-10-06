import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HistoryTab } from './HistoryTab';

const BASE = 'http://backend/api/proxy/release-copilot';

jest.mock('../api', () => ({
  ...jest.requireActual('../api'),
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
}));

const RUN = 'https://github.com/example/build/actions/runs/4242';

const HISTORY = {
  ok: true,
  days: 21,
  releases: [
    {
      release_name: 'CARE-12',
      pr_number: 31,
      released_at: '2026-10-01T10:00:00Z',
      released_by: 'dev@example.com',
      items: [
        // Qualified once: goes straight back.
        { artifact_name: 'orders-api', artifact_version: '1.4.0', jira_ticket: 'ABC-123', from_queue: true,
          build_run_url: RUN, target_envs: 'prd,prl1', queued_by: 'dev@example.com' },
        // Already in the next release: cannot be ticked.
        { artifact_name: 'ledger-api', artifact_version: '2.0.0', jira_ticket: 'ABC-124', from_queue: true,
          in_queue: true },
        // Never queued: needs its run and ticket typed in.
        { artifact_name: 'payments-api', artifact_version: '3.1.0' },
      ],
    },
    {
      release_name: 'DF-5',
      pr_number: 32,
      released_at: '2026-09-28T10:00:00Z',
      items: [
        { artifact_name: 'ingest-job', artifact_version: '0.3.0', jira_ticket: 'ABC-200', df_only: true,
          from_queue: true, target_envs: 'prd' },
      ],
    },
  ],
};

type Call = { url: string; body?: any };
let calls: Call[];
let replies: Record<string, any>;

beforeEach(() => {
  calls = [];
  window.localStorage.clear();
  replies = {
    '/api/release-history': HISTORY,
    '/api/release-queue/requeue': {
      ok: true,
      queued: [{ ok: true, artifact: 'orders-api:1.4.0' }],
      refused: [],
    },
    '/api/release-queue/batch': {
      ok: true,
      queued: [{ ok: true, artifact: 'payments-api:3.1.0' }],
      refused: [],
    },
  };
  (global as any).fetch = jest.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const path = url.slice(BASE.length).split('?')[0];
    return { ok: true, status: 200, json: async () => replies[path] ?? {} };
  });
});

const rendered = async () => {
  render(<HistoryTab />);
  await screen.findByText('CARE-12');
};
const posts = (path: string) => calls.filter(c => c.url === `${BASE}${path}`);

describe('HistoryTab', () => {
  it('reads the last three weeks and shows each release with its summary', async () => {
    await rendered();
    expect(calls[0].url).toBe(`${BASE}/api/release-history?days=21`);
    expect(screen.getByText('2 releases in the last 3 weeks')).toBeInTheDocument();
    expect(screen.getByText('DF-5')).toBeInTheDocument();
    expect(screen.getByText(/3 charts · 1 ready · 1 in next release · 1 needs run \+ ticket/)).toBeInTheDocument();
    expect(screen.getByText(/1 chart · ready/)).toBeInTheDocument();
    // The newest release is open; the second is collapsed.
    expect(screen.getByText('orders-api:1.4.0')).toBeInTheDocument();
    expect(screen.queryByText('ingest-job:0.3.0')).not.toBeInTheDocument();
    // The kind chips count every chart.
    expect(screen.getByRole('button', { name: /^All 4$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^CARE 3$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^DF 1$/ })).toBeInTheDocument();
  });

  it('says when the history is off', async () => {
    replies['/api/release-history'] = { ok: false, disabled: true, error: 'Release queue is disabled' };
    render(<HistoryTab />);
    expect(await screen.findByText('The history is off — no BigQuery dataset is configured.')).toBeInTheDocument();
  });

  it('narrows by kind and by search, and says when nothing matches', async () => {
    await rendered();
    fireEvent.click(screen.getByRole('button', { name: /^DF 1$/ }));
    expect(screen.queryByText('CARE-12')).not.toBeInTheDocument();
    expect(screen.getByText('DF-5')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^All 4$/ }));
    fireEvent.change(screen.getByLabelText('Search chart or ticket'), { target: { value: 'ABC-200' } });
    expect(screen.queryByText('CARE-12')).not.toBeInTheDocument();
    // A search opens every hit.
    expect(screen.getByText('ingest-job:0.3.0')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Search chart or ticket'), { target: { value: 'orders' } });
    expect(screen.getByText(/1 of 3 shown/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Search chart or ticket'), { target: { value: 'no-such-chart' } });
    expect(screen.getByText(/No chart matches “no-such-chart” in the last 3 weeks\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByText('CARE-12')).toBeInTheDocument();
  });

  it('Re-queue all ticks the ready rows only, and the submit puts them back directly', async () => {
    await rendered();
    expect(screen.getByRole('checkbox', { name: 'ledger-api:2.0.0' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'payments-api:3.1.0' })).toBeDisabled();

    // The newest release's button: each release has its own.
    fireEvent.click(screen.getAllByRole('button', { name: /Re-queue all \(1\)/ })[0]);
    expect(screen.getByRole('checkbox', { name: 'orders-api:1.4.0' })).toBeChecked();
    expect(screen.getByText(/· 1 ticked/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'dev@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add 1 to the next release/ }));

    expect(await screen.findByText('✅ orders-api:1.4.0 is back in the next release.')).toBeInTheDocument();
    expect(posts('/api/release-queue/requeue').map(c => c.body)).toEqual([
      { requested_by: 'dev@example.com', items: [{ artifact_name: 'orders-api', artifact_version: '1.4.0' }] },
    ]);
    expect(posts('/api/release-queue/batch')).toEqual([]);
    // It refreshed afterwards, and the ticks started over.
    await waitFor(() => expect(calls.filter(c => c.url.includes('/api/release-history')).length).toBe(2));
    expect(screen.getByRole('checkbox', { name: 'orders-api:1.4.0' })).not.toBeChecked();
  });

  it('a never-queued chart can be ticked once its run and ticket are typed, and takes the gate', async () => {
    await rendered();
    const box = screen.getByRole('checkbox', { name: 'payments-api:3.1.0' });
    fireEvent.change(screen.getByLabelText('Build run URL for payments-api:3.1.0'), { target: { value: RUN } });
    expect(box).toBeDisabled();
    fireEvent.change(screen.getByLabelText('JIRA ticket for payments-api:3.1.0'), { target: { value: ' abc-125 ' } });
    expect(box).toBeEnabled();
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('checkbox', { name: 'orders-api:1.4.0' }));

    fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'dev@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add 2 to the next release/ }));

    const flash = await screen.findByText('✅ payments-api:3.1.0 is back in the next release.');
    expect(within(flash.parentElement!).getByText('✅ orders-api:1.4.0 is back in the next release.')).toBeInTheDocument();
    expect(posts('/api/release-queue/requeue').map(c => c.body.items)).toEqual([
      [{ artifact_name: 'orders-api', artifact_version: '1.4.0' }],
    ]);
    expect(posts('/api/release-queue/batch').map(c => c.body)).toEqual([
      {
        requested_by: 'dev@example.com',
        change_details: 'Re-queued from CARE-12',
        rows: [
          {
            artifact: 'payments-api:3.1.0',
            build_run_url: RUN,
            jira_ticket: 'ABC-125',
            prl1_only: false,
            df_only: false,
            target_envs: '',
            change_details: '',
            note: '',
          },
        ],
      },
    ]);
  });

  it('asks for an email before sending anything, and names a refused chart', async () => {
    replies['/api/release-queue/requeue'] = {
      ok: false,
      queued: [],
      refused: [{ ok: false, artifact: 'orders-api:1.4.0', error: 'orders-api:1.4.0 is already queued for the next release.' }],
    };
    await rendered();
    fireEvent.click(screen.getByRole('checkbox', { name: 'orders-api:1.4.0' }));
    fireEvent.click(screen.getByRole('button', { name: /Add 1 to the next release/ }));
    expect(screen.getByText('Your email is needed — the queue records who asked.')).toBeInTheDocument();
    expect(posts('/api/release-queue/requeue')).toEqual([]);

    fireEvent.change(screen.getByLabelText('Your email'), { target: { value: 'dev@example.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Add 1 to the next release/ }));
    expect(
      await screen.findByText('❌ orders-api:1.4.0 — orders-api:1.4.0 is already queued for the next release.'),
    ).toBeInTheDocument();
  });
});
