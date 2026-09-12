import { ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { TestApiProvider } from '@backstage/test-utils';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { streamChat } from '../api';
import { useAgentChat } from './useAgentChat';

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
    expect(result.current.resultFor('dataflow')).toEqual({ text: '', streaming: false, pendingToken: null });
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
});
