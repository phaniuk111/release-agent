import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SupportTab } from './SupportTab';

jest.mock('../api', () => {
  const actual = jest.requireActual('../api');
  return { ...actual, useApiBase: () => 'http://backend/api/proxy/release-copilot' };
});

const REPORT = {
  ok: true,
  business_date: '2026-10-02',
  date_label: 'COB',
  previous_date: '2026-10-01',
  counts: { items: 120, failed: 4, stuck: 1, missing: 0, recovered: 2 },
  runbook_entries: 3,
  labels: { system: 'Feed', process: 'Report' },
  job_url: 'https://console.example.com/jobs/{job_id}',
  incidents: [
    {
      id: 'e-1a2b3c4d',
      priority: 'high',
      priority_reason: 'a whole system down',
      title: '4 failed · process · Report REPORT-B',
      facts: ['4 runs of REPORT-B failed', 'first seen 02:10'],
      error_text: 'ACCT-1 upstream file not found after a long wait for the vendor',
      job_ids: ['job-1', 'job-2'],
      shared: { system: 'SYS-A', process: 'REPORT-B' },
      runbook: 'Vendor file late',
      steps: ['Check the vendor drop', 'Re-trigger once'],
      owner: 'Feeds team',
      action: 'retrigger',
      category: 'process',
      note: 'COB 2026-10-02: 4 failed for REPORT-B',
    },
    {
      id: 'stuck',
      priority: 'low',
      title: '1 stuck',
      facts: ['SYS-A run waiting'],
      job_ids: [],
      owner: 'L1',
      action: 'wait',
      note: 'one stuck',
    },
  ],
};

type FetchCall = [string, RequestInit | undefined];
let fetchMock: jest.Mock;

function answer(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
}

beforeEach(() => {
  fetchMock = jest.fn((url: string) =>
    url.includes('/api/support/feedback') ? answer({ ok: true, recorded: true }) : answer(REPORT),
  );
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

describe('SupportTab — the L1 work list', () => {
  it('reads the latest date and renders one card per incident', async () => {
    render(<SupportTab onSend={jest.fn()} busy={false} llm />);
    expect(await screen.findByText('4 failed · process · Report REPORT-B')).toBeInTheDocument();
    expect((fetchMock.mock.calls[0] as FetchCall)[0]).toBe(
      'http://backend/api/proxy/release-copilot/api/support/triage?date=&fresh=0',
    );
    expect(screen.getAllByTestId('support-incident')).toHaveLength(2);
    expect(screen.getByText('COB 2026-10-02 · 4 failed · 1 stuck of 120 runs')).toBeInTheDocument();
    expect(
      screen.getByText('2 recovered after a retry · compared with 2026-10-01 · 3 runbook entries'),
    ).toBeInTheDocument();
    expect(screen.getByText('→ Re-trigger once')).toBeInTheDocument();
    expect(screen.getByText('Vendor file late')).toBeInTheDocument();
    expect(screen.getByText('no runbook match')).toBeInTheDocument();
    expect(screen.getByText('job-1').closest('a')).toHaveAttribute(
      'href',
      'https://console.example.com/jobs/job-1',
    );
    const err = screen.getByText(/ACCT-1 upstream file not found/);
    expect(err).toHaveAttribute('title', REPORT.incidents[0].error_text);
    expect(screen.getByLabelText('Business date')).toHaveValue('2026-10-02');
  });

  it('hides Investigate and Ask why without a model', async () => {
    render(<SupportTab onSend={jest.fn()} busy={false} llm={false} />);
    await screen.findByText('4 failed · process · Report REPORT-B');
    expect(screen.queryByText('Investigate')).not.toBeInTheDocument();
    expect(screen.queryByText('Ask why')).not.toBeInTheDocument();
    expect(screen.getAllByText('Copy ticket note')).toHaveLength(2);
  });

  it('Investigate sends the incident id, its date and its shared values', async () => {
    const onSend = jest.fn();
    render(<SupportTab onSend={onSend} busy={false} llm />);
    await screen.findByText('4 failed · process · Report REPORT-B');
    fireEvent.click(screen.getAllByText('Investigate')[0]);
    expect(onSend).toHaveBeenCalledWith(
      'Investigate incident e-1a2b3c4d · COB 2026-10-02 · 4 failed · process · Report REPORT-B · ' +
        'Feed SYS-A · Report REPORT-B · jobs job-1, job-2. Why did it fail, and what should L1 do?',
    );
    fireEvent.click(screen.getAllByText('Ask why')[1]);
    expect(onSend).toHaveBeenLastCalledWith(expect.stringContaining('explain incident "1 stuck"'));
  });

  it('re-reads when the date changes', async () => {
    render(<SupportTab onSend={jest.fn()} busy={false} llm />);
    await screen.findByText('4 failed · process · Report REPORT-B');
    fireEvent.change(screen.getByLabelText('Business date'), { target: { value: '2026-09-30' } });
    await waitFor(() =>
      expect(fetchMock.mock.calls.map(c => c[0])).toContain(
        'http://backend/api/proxy/release-copilot/api/support/triage?date=2026-09-30&fresh=0',
      ),
    );
  });

  it('says what to set when triage is disabled, and the error and hint when it fails', async () => {
    fetchMock.mockImplementationOnce(() => answer({ ok: false, disabled: true }));
    const { unmount } = render(<SupportTab onSend={jest.fn()} busy={false} llm />);
    expect(await screen.findByText(/To enable it, set SUPPORT_TABLE/)).toBeInTheDocument();
    expect(screen.getByText('Support triage is disabled')).toBeInTheDocument();
    unmount();

    fetchMock.mockImplementationOnce(() => answer({ ok: false, error: 'Access denied', hint: 'Grant Data Viewer.' }));
    render(<SupportTab onSend={jest.fn()} busy={false} llm />);
    expect(await screen.findByText('Access denied')).toBeInTheDocument();
    expect(screen.getByText('Grant Data Viewer.')).toBeInTheDocument();
  });

  it('says an empty date has nothing for L1', async () => {
    fetchMock.mockImplementationOnce(() =>
      answer({ ok: true, empty: true, business_date: '2026-10-04', counts: { items: 0 }, incidents: [] }),
    );
    render(<SupportTab onSend={jest.fn()} busy={false} llm />);
    expect(await screen.findByText('✓ No runs for 2026-10-04 in the control table.')).toBeInTheDocument();
  });

  it('asks "Was this right?" only after a complete investigation, and saves the answer', async () => {
    const { rerender } = render(
      <SupportTab
        onSend={jest.fn()}
        busy={false}
        llm
        result={{ text: 'The vendor file was late.', streaming: false, pendingToken: null }}
      />,
    );
    await screen.findByText('4 failed · process · Report REPORT-B');
    expect(screen.getByText('The vendor file was late.')).toBeInTheDocument();
    expect(screen.queryByText('Was this right?')).not.toBeInTheDocument();

    const investigation = {
      business_date: '2026-10-02',
      incident_id: 'e-1a2b3c4d',
      title: '4 failed · process · Report REPORT-B',
      model: 'm-1',
      model_calls: 1,
      seconds: 4.2,
    };
    rerender(
      <SupportTab
        onSend={jest.fn()}
        busy={false}
        llm
        result={{ text: 'The vendor file was late.', streaming: false, pendingToken: null }}
        investigation={investigation}
      />,
    );
    fireEvent.click(screen.getByText('Wrong'));
    fireEvent.change(screen.getByLabelText('What was the actual cause?'), {
      target: { value: '  A re-sent file  ' },
    });
    fireEvent.click(screen.getByText('Save'));
    expect(
      await screen.findByText("✓ Thanks — marked wrong. What you wrote is kept for the team's runbook."),
    ).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(c => String(c[0]).endsWith('/api/support/feedback')) as FetchCall;
    expect(post[1]?.method).toBe('POST');
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      business_date: '2026-10-02',
      incident_id: 'e-1a2b3c4d',
      title: '4 failed · process · Report REPORT-B',
      verdict: 'wrong',
      actual_cause: 'A re-sent file',
      category: '',
      action: '',
      model: 'm-1',
      model_calls: 1,
      seconds: 4.2,
    });
  });
});
