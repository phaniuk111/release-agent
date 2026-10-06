import { ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { TestApiProvider } from '@backstage/test-utils';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { streamChat } from '../api';
import { interruptKind, useAgentChat } from './useAgentChat';

jest.mock('../api', () => ({
  useApiBase: () => 'http://backend/api/proxy/release-copilot',
  streamChat: jest.fn(),
}));

const mockStream = streamChat as jest.MockedFunction<typeof streamChat>;

/** The next turn's reply, streamed in two chunks. */
function replyWith(text: string) {
  mockStream.mockImplementationOnce(async (_base, _msg, _thread, onEvent) => {
    const half = Math.ceil(text.length / 2);
    onEvent({ type: 'token', content: text.slice(0, half) });
    onEvent({ type: 'token', content: text.slice(half) });
  });
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <TestApiProvider apis={[[toastApiRef, { post: jest.fn() }]]}>{children}</TestApiProvider>
);

beforeEach(() => {
  mockStream.mockReset();
  window.sessionStorage.clear();
});

describe('useAgentChat', () => {
  it('keeps one conversation for the page instead of a new one per message', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    replyWith('first');
    replyWith('second');
    await act(() => result.current.send('hello'));
    await act(() => result.current.send('and then?'));
    const [first, second] = mockStream.mock.calls.map(c => c[2]);
    expect(first).toMatch(/^backstage-release-/);
    expect(second).toBe(first);
  });

  it('shows a form tab the reply to its own submission, and its token', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    replyWith('Deploy payments-api:1.2.2 to UAT. Reply CONFIRM-ab12cd to confirm.');
    await act(() => result.current.send('{"environment":"uat"}', { origin: 'deploy' }));

    const deploy = result.current.resultFor('deploy');
    expect(deploy.text).toContain('Deploy payments-api:1.2.2');
    expect(deploy.pendingToken).toBe('CONFIRM-AB12CD');
    expect(deploy.streaming).toBe(false);
    // Another tab sees neither the reply nor the token.
    expect(result.current.resultFor('dataflow')).toEqual({
      text: '',
      streaming: false,
      pendingToken: null,
      progress: [],
    });
  });

  it('confirms with the pending token, and the outcome lands where the preview was', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    replyWith('Preview. Reply CONFIRM-ab12cd to confirm.');
    await act(() => result.current.send('{"environment":"uat"}', { origin: 'deploy' }));
    replyWith('Deployed payments-api:1.2.2 to UAT.');
    await act(async () => result.current.confirm());

    expect(mockStream.mock.calls[1][1]).toBe('CONFIRM-AB12CD');
    const deploy = result.current.resultFor('deploy');
    expect(deploy.text).toBe('Deployed payments-api:1.2.2 to UAT.');
    expect(deploy.pendingToken).toBeNull();
  });

  it('logs what the person asked, not the routing context sent with it', async () => {
    const { result } = renderHook(() => useAgentChat('onboarding'), { wrapper });
    replyWith('Not documented yet — ask the API owner.');
    await act(() =>
      result.current.send('Consumer onboarding question: How do I get access?', {
        display: 'How do I get access?',
      }),
    );
    expect(mockStream.mock.calls[0][1]).toBe('Consumer onboarding question: How do I get access?');
    expect(result.current.messages[0]).toEqual({ role: 'user', text: 'How do I get access?' });
  });

  it('takes the token from the agent\'s interrupt, not from the reply text', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    mockStream.mockImplementationOnce(async (_b, _m, _t, onEvent) => {
      onEvent({ type: 'progress', content: 'Building the preview' });
      onEvent({ type: 'token', content: 'Release preview ready.' });
      onEvent({ type: 'interrupt', data: { type: 'confirmation', token: 'confirm-00fb2c', message: 'Reply …' } });
    });
    await act(() => result.current.send('{"deployment_type":"release"}', { origin: 'releases' }));
    expect(result.current.resultFor('releases').pendingToken).toBe('CONFIRM-00FB2C');
    expect(result.current.approval).toBeNull();
  });

  it('a yes/no approval asks Approve or Reject, and answers it with yes or no', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    mockStream.mockImplementationOnce(async (_b, _m, _t, onEvent) => {
      onEvent({
        type: 'interrupt',
        data: {
          type: 'confirmation',
          message: 'Promote the current release\'s file-set to **PRD**?',
          action: 'Reply "yes" to approve, anything else to reject.',
          function: 'promote_release',
        },
      });
    });
    await act(() => result.current.send('promote the CARE release to prd'));
    expect(result.current.approval?.message).toContain('to **PRD**?');
    expect(result.current.resultFor('chat').pendingToken).toBeNull();

    replyWith('Release file-set promoted to PRD via PR #9 (merged).');
    await act(async () => result.current.approve());
    expect(mockStream.mock.calls[1][1]).toBe('yes');
    expect(result.current.approval).toBeNull();
    expect(result.current.resultFor('chat').text).toContain('promoted to PRD');
  });

  it('cancelling a preview tells the agent no, so it stops waiting', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    replyWith('Preview. Reply CONFIRM-ab12cd to confirm.');
    await act(() => result.current.send('{"environment":"uat"}', { origin: 'deploy' }));
    replyWith('Cancelled — nothing was applied.');
    await act(async () => result.current.dismiss());
    expect(mockStream.mock.calls[1][1]).toBe('no');
    expect(result.current.pending).toBeNull();
  });

  it('keeps a complete investigation for the tab that asked', async () => {
    const { result } = renderHook(() => useAgentChat('release'), { wrapper });
    mockStream.mockImplementationOnce(async (_b, _m, _t, onEvent) => {
      onEvent({ type: 'token', content: 'Finding: the feed file did not arrive.' });
      onEvent({ type: 'investigation', data: { business_date: '2026-10-02', incident_id: 'e-1a2b3c4d' } });
    });
    await act(() => result.current.send('Investigate incident e-1a2b3c4d', { origin: 'support' }));
    expect(result.current.investigationFor('support')?.incident_id).toBe('e-1a2b3c4d');
    expect(result.current.investigationFor('chat')).toBeNull();
  });
});

describe('interruptKind', () => {
  it('tells a CONFIRM token from a yes/no approval, never mixing them', () => {
    expect(interruptKind({ token: 'confirm-ab12', message: 'x' })).toEqual({ token: 'CONFIRM-AB12' });
    expect(interruptKind({ function: 'promote_release', message: 'Promote?' })).toEqual({ approval: 'Promote?' });
    expect(interruptKind({ message: 'Ok?', action: 'Reply "yes" to approve' })).toEqual({ approval: 'Ok?' });
    expect(interruptKind({ type: 'budget_confirmation', message: 'Reply "yes"' })).toBeNull();
    expect(interruptKind(null)).toBeNull();
  });
});
