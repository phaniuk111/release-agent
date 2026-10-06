import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { apiGet, apiPost } from '../api';
import { ReleasesTab } from './ReleasesTab';

jest.mock('../api', () => ({
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
  apiGet: jest.fn(),
  apiPost: jest.fn(),
}));

const mockGet = apiGet as jest.MockedFunction<typeof apiGet>;
const mockPost = apiPost as jest.MockedFunction<typeof apiPost>;
const field = (id: string) => document.getElementById(id) as HTMLInputElement;

const QUEUE = [
  {
    artifact_name: 'orders-api',
    artifact_version: '1.2.0',
    requested_by: 'dev@example.com',
    build_verified: true,
    jira_ticket: 'ABC-101',
    prl1_only: false,
    df_only: false,
  },
  {
    artifact_name: 'payments-api',
    artifact_version: '3.0.1',
    requested_by: 'dev@example.com',
    build_verified: false,
    prl1_only: true,
    df_only: false,
  },
  {
    artifact_name: 'orders-df',
    artifact_version: '1.4.2',
    requested_by: 'dev@example.com',
    df_only: true,
    target_envs: 'prd',
  },
];

function ctx(over: Record<string, unknown> = {}) {
  return {
    queue: QUEUE,
    default_repo: 'example-org/deployment-repo',
    df_default_repo: 'example-org/df-release-repo',
    care_release_mode: 'fileset',
    care_release_repo: '',
    care_release_file: '',
    ...over,
  };
}

const DEFAULTS = {
  ok: true,
  number: 32,
  fields: {
    release_name: 'October 8th 2026 : Release 32',
    change_summary: 'Release 32',
    change_description: 'orders-api and payments-api.',
    change_reason: 'Queued changes.',
    associated_risk: 'Low risk.',
    consequence: 'The fixes wait.',
    user_service_impact: 'No user impact is expected.',
  },
};

/** The agent's answers: the queue, who is signed in, the defaults and the draft. */
function serve(
  queueCtx: Record<string, unknown>,
  draft?: Record<string, unknown>,
) {
  mockGet.mockImplementation(async (_base: string, path: string) => {
    if (path === '/api/release-queue') return queueCtx as never;
    if (path === '/api/whoami') return { signed_in: false } as never;
    throw new Error(`unexpected GET ${path}`);
  });
  mockPost.mockImplementation(async (_base: string, path: string) => {
    if (path === '/api/release-defaults') return DEFAULTS as never;
    if (path === '/api/release-draft')
      return (draft ?? { ok: false, error: 'no draft' }) as never;
    throw new Error(`unexpected POST ${path}`);
  });
}

const create = (kind: 'CARE' | 'DF') =>
  screen.getByRole('button', { name: `Create ${kind} release` });

function fillTimes() {
  fireEvent.change(field('rel-start'), {
    target: { value: '2026-10-08T18:00' },
  });
  fireEvent.change(field('rel-end'), { target: { value: '2026-10-08T20:00' } });
  fireEvent.change(field('rel-initiator'), {
    target: { value: 'dev@example.com' },
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  window.localStorage.clear();
});

describe('ReleasesTab — the CARE / DF release form, as the portal’s release form', () => {
  it('ticks this kind’s queued items and fills the change request from the defaults', async () => {
    serve(ctx());
    render(<ReleasesTab onSend={jest.fn()} />);
    expect(await screen.findByTestId('release-queued')).toHaveTextContent(
      'Queued for this release (2) — untick to defer to the next one',
    );
    expect(screen.queryByText(/orders-df/)).toBeNull();
    expect(field('rel-artifacts').value).toBe(
      'orders-api:1.2.0\npayments-api:3.0.1',
    );
    expect(field('rel-repo').value).toBe('example-org/deployment-repo');
    await waitFor(() =>
      expect(field('rel-name').value).toBe('October 8th 2026 : Release 32'),
    );
    expect(field('rel-summary').value).toBe('Release 32');
    expect(field('rel-impact').value).toBe('No user impact is expected.');
    expect(mockPost).toHaveBeenCalledWith(
      expect.any(String),
      '/api/release-defaults',
      expect.objectContaining({
        artifacts: ['orders-api:1.2.0', 'payments-api:3.0.1'],
        kind: 'care',
      }),
      { allowFailure: true },
    );
    // Routing is read from the queue, not asked again.
    expect(screen.getByTestId('release-routing')).toHaveTextContent(
      'UAT → PRL1 · PRL1-only, held back from PRD',
    );
    // Fileset CARE drafts from a button, never on its own.
    expect(
      screen.getByRole('button', { name: 'Draft change request' }),
    ).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalledWith(
      expect.any(String),
      '/api/release-draft',
      expect.anything(),
      expect.anything(),
    );
  });

  it('defers an unticked item, needs a JIRA, and sends the portal’s exact payload', async () => {
    serve(ctx());
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<ReleasesTab onSend={onSend} />);
    await waitFor(() =>
      expect(field('rel-name').value).toBe('October 8th 2026 : Release 32'),
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'orders-api:1.2.0' }));
    expect(field('rel-artifacts').value).toBe('payments-api:3.0.1');
    fillTimes();

    fireEvent.click(create('CARE'));
    expect(await screen.findByTestId('release-error')).toHaveTextContent(
      'JIRA is required — every commit message of this change starts with it.',
    );
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(field('rel-jira'), { target: { value: ' ABC-123 ' } });
    fireEvent.click(create('CARE'));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0])).toEqual({
      deployment_repo: 'example-org/deployment-repo',
      release_name: 'October 8th 2026 : Release 32',
      start_date: '2026-10-08 18:00:00',
      end_date: '2026-10-08 20:00:00',
      change_initiator: 'dev@example.com',
      jira: 'ABC-123',
      change_summary: 'Release 32',
      change_description: 'orders-api and payments-api.',
      change_reason: 'Queued changes.',
      associated_risk: 'Low risk.',
      consequence: 'The fixes wait.',
      user_service_impact: 'No user impact is expected.',
      prl1_only: ['payments-api'],
      df_images: [],
      artefact: ['payments-api:3.0.1'],
      release_kind: 'care',
    });
    expect(
      window.localStorage.getItem('release-copilot:release-initiator'),
    ).toBe('dev@example.com');
  });

  it('keeps what the person typed when the defaults recompute', async () => {
    serve(ctx());
    render(<ReleasesTab onSend={jest.fn()} />);
    await waitFor(() => expect(field('rel-summary').value).toBe('Release 32'));
    fireEvent.change(field('rel-summary'), {
      target: { value: 'My own summary' },
    });
    fireEvent.change(field('rel-start'), {
      target: { value: '2026-10-09T18:00' },
    });
    await waitFor(() =>
      expect(
        mockPost.mock.calls.filter(c => c[1] === '/api/release-defaults'),
      ).toHaveLength(2),
    );
    // The second call sends back the number the first one found.
    expect(mockPost.mock.calls[1][2]).toEqual(
      expect.objectContaining({ date: '2026-10-09', number: 32 }),
    );
    expect(field('rel-summary').value).toBe('My own summary');
  });

  it('fileset CARE drafts from its button, from the ticked items only', async () => {
    serve(ctx(), {
      ok: true,
      draft: {
        change_summary: 'payments-api 3.0.1 fixes refunds.',
        user_impact: 'None expected.',
      },
      grounded_on: 1,
    });
    render(<ReleasesTab onSend={jest.fn()} />);
    await waitFor(() => expect(field('rel-summary').value).toBe('Release 32'));
    fireEvent.click(screen.getByRole('checkbox', { name: 'orders-api:1.2.0' }));
    fireEvent.click(
      screen.getByRole('button', { name: 'Draft change request' }),
    );
    await waitFor(() =>
      expect(field('rel-summary').value).toBe(
        'payments-api 3.0.1 fixes refunds.',
      ),
    );
    expect(mockPost).toHaveBeenCalledWith(
      expect.any(String),
      '/api/release-draft',
      { artifacts: ['payments-api:3.0.1'], kind: 'care' },
      { allowFailure: true },
    );
    expect(field('rel-impact').value).toBe('None expected.');
    expect(field('rel-reason').value).toBe('Queued changes.');
    expect(screen.getByTestId('draft-note')).toHaveTextContent(
      'Draft from 1 item(s) — review every field before submitting.',
    );
  });

  it('offers PRL1-only for a chart typed in that was never queued', async () => {
    serve(ctx({ queue: [QUEUE[2]] }));
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<ReleasesTab onSend={onSend} />);
    expect(await screen.findByTestId('release-empty')).toHaveTextContent(
      'Nothing is queued for the CARE release yet — 1 item is queued for the Dataflow release.',
    );
    fireEvent.change(field('rel-artifacts'), {
      target: { value: 'ledger-api:2.0.0' },
    });
    expect(screen.getByTestId('release-routing')).toHaveTextContent(
      'not queued — no routing choice to read',
    );
    fireEvent.click(
      screen.getByRole('checkbox', { name: 'ledger-api PRL1-only' }),
    );
    await waitFor(() =>
      expect(field('rel-name').value).toBe('October 8th 2026 : Release 32'),
    );
    fillTimes();
    fireEvent.change(field('rel-jira'), { target: { value: 'ABC-123' } });
    fireEvent.click(create('CARE'));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0]).prl1_only).toEqual([
      'ledger-api',
    ]);
  });

  it('a DF release opens drafted, marks each field, and sends its images as df_images', async () => {
    serve(ctx(), {
      ok: true,
      draft: {
        change_summary: 'orders-df 1.4.2 ships the new enrichment.',
        change_description: 'orders-df adds the enrichment step.',
        change_reason: 'Requested by the team.',
        associated_risk: 'Low risk.',
        consequence: 'The enrichment waits.',
        user_service_impact: 'No user impact is expected.',
      },
      sources: {
        change_summary: 'ai',
        change_description: 'ai',
        change_reason: 'fallback',
        associated_risk: 'team',
        consequence: 'team',
        user_service_impact: 'team',
      },
      grounded_on: 1,
    });
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<ReleasesTab onSend={onSend} />);
    await screen.findByTestId('release-queued');
    fireEvent.mouseDown(document.getElementById('release-kind') as HTMLElement);
    fireEvent.click(
      await screen.findByRole('option', { name: 'Dataflow release' }),
    );

    await waitFor(() =>
      expect(field('rel-artifacts').value).toBe('orders-df:1.4.2'),
    );
    expect(field('rel-repo').value).toBe('example-org/df-release-repo');
    await waitFor(() =>
      expect(field('rel-summary').value).toBe(
        'orders-df 1.4.2 ships the new enrichment.',
      ),
    );
    expect(mockPost).toHaveBeenCalledWith(
      expect.any(String),
      '/api/release-draft',
      { artifacts: ['orders-df:1.4.2'], kind: 'df' },
      { allowFailure: true },
    );
    expect(screen.getByTestId('chip-change_summary')).toHaveTextContent(
      'AI draft',
    );
    expect(screen.getByTestId('chip-change_reason')).toHaveTextContent(
      'Fallback',
    );
    expect(screen.getByTestId('draft-note')).toHaveTextContent(
      'AI draft of 1 item — review every field; anything you edit is kept.',
    );
    expect(
      screen.getByRole('button', { name: 'Regenerate' }),
    ).toBeInTheDocument();
    // The defaults land after the draft and must not undo it.
    await waitFor(() =>
      expect(field('rel-name').value).toBe('October 8th 2026 : Release 32'),
    );
    expect(field('rel-summary').value).toBe(
      'orders-df 1.4.2 ships the new enrichment.',
    );
    fireEvent.change(field('rel-reason'), { target: { value: 'Our reason.' } });
    expect(screen.getByTestId('chip-change_reason')).toHaveTextContent(
      'Edited',
    );
    expect(screen.getByTestId('release-routing')).toHaveTextContent(
      'PRD pipeline — triggered at deploy time',
    );

    fillTimes();
    fireEvent.change(field('rel-jira'), { target: { value: 'ABC-123' } });
    fireEvent.click(create('DF'));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const sent = JSON.parse(onSend.mock.calls[0][0]);
    expect(sent).toEqual(
      expect.objectContaining({
        deployment_repo: 'example-org/df-release-repo',
        change_reason: 'Our reason.',
        prl1_only: [],
        df_images: ['orders-df'],
        artefact: ['orders-df:1.4.2'],
        release_kind: 'df',
        jira: 'ABC-123',
      }),
    );
  });

  it('CARE mono mode: one file in its repo, the summary follows the name', async () => {
    serve(
      ctx({
        care_release_mode: 'mono',
        care_release_repo: 'example-org/mono-repo',
        care_release_file: 'release_details.json',
      }),
      { ok: false, error: 'Could not draft — the standard wording stays.' },
    );
    render(<ReleasesTab onSend={jest.fn()} />);
    expect(
      await screen.findByText(
        /One file — release_details\.json in example-org\/mono-repo\. The portal raises a PR/,
      ),
    ).toBeInTheDocument();
    expect(field('rel-repo').value).toBe('example-org/mono-repo');
    expect(field('rel-repo')).toHaveAttribute('readonly');
    await waitFor(() =>
      expect(field('rel-summary').value).toBe('October 8th 2026 : Release 32'),
    );
    expect(screen.getByTestId('draft-note')).toHaveTextContent(
      'Could not draft — the standard wording stays.',
    );
    await waitFor(() =>
      expect(screen.getByTestId('chip-change_description')).toHaveTextContent(
        'Team wording',
      ),
    );
    fireEvent.change(field('rel-name'), {
      target: { value: 'October 9th 2026 : Release 32' },
    });
    expect(field('rel-summary').value).toBe('October 9th 2026 : Release 32');
  });

  it('with no model, offers no drafting and keeps the standard wording', async () => {
    serve(
      ctx({
        care_release_mode: 'mono',
        care_release_repo: 'example-org/mono-repo',
      }),
    );
    render(<ReleasesTab onSend={jest.fn()} llm={false} />);
    await waitFor(() =>
      expect(field('rel-desc').value).toBe('orders-api and payments-api.'),
    );
    expect(screen.queryByRole('button', { name: 'Regenerate' })).toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Draft change request' }),
    ).toBeNull();
    expect(mockPost.mock.calls.some(c => c[1] === '/api/release-draft')).toBe(
      false,
    );
  });

  it('shows the preview and confirms its token in place', async () => {
    serve(ctx());
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(
      <ReleasesTab
        onSend={jest.fn()}
        busy={false}
        result={{
          text: 'Release preview. Reply CONFIRM-ABC123.',
          streaming: false,
          pendingToken: 'CONFIRM-ABC123',
        }}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(await screen.findByTestId('inline-confirm')).toHaveTextContent(
      'CONFIRM-ABC123',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Confirm & release' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
