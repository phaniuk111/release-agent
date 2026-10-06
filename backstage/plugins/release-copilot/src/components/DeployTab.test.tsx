import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { apiGet } from '../api';
import { DeployTab } from './DeployTab';

jest.mock('../api', () => ({
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
  apiGet: jest.fn(),
}));

const mockGet = apiGet as jest.MockedFunction<typeof apiGet>;

const ENTRY = { helm_chart_name: 'orders-api', helm_chart_version: '1.2.0' };

function template(over: Record<string, unknown> = {}) {
  return {
    environment: 'uat',
    deployment: { include: [ENTRY] },
    from_repo: true,
    branch: 'SIT',
    blocked: '',
    deploy_repo: 'example-org/deployment-repo',
    ...over,
  };
}

const field = (id: string) => document.getElementById(id) as HTMLInputElement;

beforeEach(() => mockGet.mockReset());

describe('DeployTab — Deploy to CARE UAT, as the portal’s deploy form', () => {
  it('opens on the live file (SIT’s, where UAT changes flow through) and asks only UAT', async () => {
    mockGet.mockResolvedValueOnce(template() as never);
    render(<DeployTab onSend={jest.fn()} />);
    await waitFor(() =>
      expect(field('deploy-json').value).toContain('orders-api'),
    );
    expect(mockGet).toHaveBeenCalledWith(
      expect.any(String),
      '/api/deploy-template?env=uat',
    );
    expect(
      screen.getByText(
        /current uat\/deployment\.json on SIT; .*OVERRIDES the file/,
      ),
    ).toBeInTheDocument();
    expect(field('deploy-repo').value).toBe('example-org/deployment-repo');
    expect(screen.queryByText(/PRD/)).toBeNull();
  });

  it('needs a JIRA before anything is sent, then sends exactly the editor’s entries', async () => {
    mockGet.mockResolvedValueOnce(template() as never);
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<DeployTab onSend={onSend} />);
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Deploy to CARE UAT' }),
      ).toBeEnabled(),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Deploy to CARE UAT' }));
    expect(await screen.findByTestId('deploy-error')).toHaveTextContent(
      'JIRA is required — every commit message of this change starts with it.',
    );
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(field('deploy-jira'), { target: { value: '  ABC-123 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy to CARE UAT' }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0])).toEqual({
      environment: 'uat',
      include: [ENTRY],
      deployment_repo: 'example-org/deployment-repo',
      jira: 'ABC-123',
    });
  });

  it('recovers entries pasted without commas, and shows what will be written', async () => {
    mockGet.mockResolvedValueOnce(template() as never);
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<DeployTab onSend={onSend} />);
    await waitFor(() =>
      expect(field('deploy-json').value).toContain('orders-api'),
    );
    fireEvent.change(field('deploy-json'), {
      target: {
        value:
          '{"helm_chart_name": "orders-api", "helm_chart_version": "1.2.1"}\n' +
          '{"helm_chart_name": "payments-api", "helm_chart_version": "3.0.1"}',
      },
    });
    fireEvent.change(field('deploy-jira'), { target: { value: 'ABC-123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy to CARE UAT' }));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    const sent = JSON.parse(onSend.mock.calls[0][0]);
    expect(
      sent.include.map((e: { helm_chart_name: string }) => e.helm_chart_name),
    ).toEqual(['orders-api', 'payments-api']);
    expect(JSON.parse(field('deploy-json').value)).toEqual({
      include: sent.include,
    });
  });

  it('refuses entries without a version, in the portal’s words', async () => {
    mockGet.mockResolvedValueOnce(template() as never);
    const onSend = jest.fn();
    render(<DeployTab onSend={onSend} />);
    await waitFor(() =>
      expect(field('deploy-json').value).toContain('orders-api'),
    );
    fireEvent.change(field('deploy-json'), {
      target: {
        value: JSON.stringify({
          include: [{ helm_chart_name: 'orders-api', helm_chart_version: '' }],
        }),
      },
    });
    fireEvent.change(field('deploy-jira'), { target: { value: 'ABC-123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy to CARE UAT' }));
    expect(await screen.findByTestId('deploy-error')).toHaveTextContent(
      'Each entry needs a non-empty helm_chart_name + helm_chart_version.',
    );
    expect(onSend).not.toHaveBeenCalled();
  });

  it('says it is blocked as the form opens, keeps submit off, and checks again on request', async () => {
    mockGet.mockResolvedValueOnce(
      template({
        blocked:
          'An open PR into SIT changes uat/deployment.json — **merge or close it** first.',
      }) as never,
    );
    render(<DeployTab onSend={jest.fn()} />);
    expect(await screen.findByTestId('deploy-blocked')).toHaveTextContent(
      'An open PR into SIT changes uat/deployment.json — merge or close it first.',
    );
    expect(
      screen.getByRole('button', { name: 'Deploy to CARE UAT' }),
    ).toBeDisabled();

    mockGet.mockResolvedValueOnce(template() as never);
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() =>
      expect(screen.queryByTestId('deploy-blocked')).toBeNull(),
    );
    expect(
      screen.getByRole('button', { name: 'Deploy to CARE UAT' }),
    ).toBeEnabled();
  });

  it('still works when the live file cannot be read, and says so', async () => {
    mockGet.mockRejectedValueOnce(
      new Error('GET /api/deploy-template: HTTP 502'),
    );
    render(<DeployTab onSend={jest.fn()} />);
    expect(await screen.findByTestId('deploy-load-note')).toHaveTextContent(
      "Couldn't load the live deployment.json (GET /api/deploy-template: HTTP 502)",
    );
    expect(JSON.parse(field('deploy-json').value)).toEqual({
      include: [{ helm_chart_name: '', helm_chart_version: '' }],
    });
  });

  it('confirms the preview’s token right under it', async () => {
    mockGet.mockResolvedValueOnce(template() as never);
    const onConfirm = jest.fn();
    render(
      <DeployTab
        onSend={jest.fn()}
        result={{
          text: 'Preview. Reply CONFIRM-ABC123.',
          streaming: false,
          pendingToken: 'CONFIRM-ABC123',
        }}
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Confirm & deploy' }),
    );
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
