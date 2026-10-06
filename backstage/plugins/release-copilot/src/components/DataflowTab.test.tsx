import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { apiGet } from '../api';
import { DataflowTab } from './DataflowTab';

jest.mock('../api', () => ({
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
  apiGet: jest.fn(),
}));

const mockGet = apiGet as jest.MockedFunction<typeof apiGet>;
const field = (id: string) => document.getElementById(id) as HTMLInputElement;
const deploy = () => screen.getByRole('button', { name: 'Deploy to DF UAT' });

const TEMPLATE = {
  environment: 'uat',
  deploy_repo: 'example-org/df-deploy',
  workflow: 'df-deploy.yml',
  fields: {
    image: { name: 'module', label: 'Module', options: [], description: '' },
    tag: {
      name: 'binary_version',
      label: 'Binary version',
      options: [],
      description: '',
    },
  },
  composer_repo: 'example-org/composer-dags',
  composer_dir: 'dags/uat',
};

beforeEach(() => mockGet.mockReset());

describe('DataflowTab — Deploy to DF UAT, as the portal’s DF deploy form', () => {
  it('labels the fields with the workflow’s own inputs and echoes the dispatch', async () => {
    mockGet.mockResolvedValueOnce(TEMPLATE as never);
    render(<DataflowTab onSend={jest.fn()} />);
    expect(
      await screen.findByText(/Triggers the df-deploy\.yml workflow/),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(field('df-composer-repo').value).toBe('example-org/composer-dags'),
    );
    expect(screen.getByLabelText('Module')).toBeInTheDocument();
    expect(screen.getByLabelText('Binary version')).toBeInTheDocument();
    fireEvent.change(field('df-image'), { target: { value: 'orders-df' } });
    fireEvent.change(field('df-tag'), { target: { value: '1.4.2' } });
    expect(screen.getByTestId('df-echo')).toHaveTextContent(
      '↳ will dispatch module=orders-df binary_version=1.4.2 → uat ✓',
    );
  });

  it('needs both values and a JIRA, in the portal’s words', async () => {
    mockGet.mockResolvedValueOnce(TEMPLATE as never);
    const onSend = jest.fn();
    render(<DataflowTab onSend={onSend} />);
    await waitFor(() =>
      expect(field('df-composer-repo').value).toBe('example-org/composer-dags'),
    );
    fireEvent.click(deploy());
    expect(await screen.findByTestId('df-error')).toHaveTextContent(
      'Module and binary version are both required.',
    );
    fireEvent.change(field('df-image'), { target: { value: 'orders-df' } });
    fireEvent.change(field('df-tag'), { target: { value: '1.4.2' } });
    fireEvent.click(deploy());
    expect(await screen.findByTestId('df-error')).toHaveTextContent(
      'JIRA is required — every commit message of this change starts with it.',
    );
    expect(onSend).not.toHaveBeenCalled();
  });

  it('sends the dispatch with its JIRA, DAG files and repos', async () => {
    mockGet.mockResolvedValueOnce(TEMPLATE as never);
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<DataflowTab onSend={onSend} />);
    await waitFor(() =>
      expect(field('df-composer-repo').value).toBe('example-org/composer-dags'),
    );
    fireEvent.change(field('df-image'), { target: { value: ' orders-df ' } });
    fireEvent.change(field('df-tag'), { target: { value: '1.4.2' } });
    fireEvent.change(field('df-jira'), { target: { value: ' ABC-123 ' } });
    fireEvent.change(field('df-dags'), {
      target: { value: 'orders-alpha.py\n\n orders-beta.py ' },
    });
    fireEvent.click(deploy());
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0])).toEqual({
      deployment_type: 'dataflow',
      environment: 'uat',
      image: 'orders-df',
      tag: '1.4.2',
      jira: 'ABC-123',
      dag_files: ['orders-alpha.py', 'orders-beta.py'],
      composer_repo: 'example-org/composer-dags',
      deployment_repo: 'example-org/df-deploy',
    });
  });

  it('asks for the Composer repo only when DAG files are named', async () => {
    mockGet.mockResolvedValueOnce({ ...TEMPLATE, composer_repo: '' } as never);
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<DataflowTab onSend={onSend} />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    fireEvent.change(field('df-image'), { target: { value: 'orders-df' } });
    fireEvent.change(field('df-tag'), { target: { value: '1.4.2' } });
    fireEvent.change(field('df-jira'), { target: { value: 'ABC-123' } });
    fireEvent.change(field('df-dags'), {
      target: { value: 'orders-alpha.py' },
    });
    fireEvent.click(deploy());
    expect(await screen.findByTestId('df-error')).toHaveTextContent(
      'Name the Composer DAGs repo (owner/repo) for those DAG files.',
    );
    fireEvent.change(field('df-dags'), { target: { value: '' } });
    fireEvent.click(deploy());
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0])).not.toHaveProperty('dag_files');
  });

  it('opens a choice input on the workflow’s default and sends it', async () => {
    mockGet.mockResolvedValueOnce({
      ...TEMPLATE,
      fields: {
        image: {
          name: 'module',
          label: 'Module',
          options: ['orders-df', 'payments-df'],
          default: 'payments-df',
        },
        tag: TEMPLATE.fields.tag,
      },
    } as never);
    const onSend = jest.fn().mockResolvedValue(undefined);
    render(<DataflowTab onSend={onSend} />);
    // A choice is a dropdown (MUI Select renders a button), not free text.
    expect(
      await screen.findByRole('button', { name: /payments-df/ }),
    ).toBeInTheDocument();
    fireEvent.change(field('df-tag'), { target: { value: '2.0.0' } });
    fireEvent.change(field('df-jira'), { target: { value: 'ABC-123' } });
    fireEvent.click(deploy());
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(JSON.parse(onSend.mock.calls[0][0]).image).toBe('payments-df');
  });
});
