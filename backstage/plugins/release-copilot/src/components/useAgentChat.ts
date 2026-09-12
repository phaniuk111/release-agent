import { useCallback, useRef, useState } from 'react';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { useApi } from '@backstage/core-plugin-api';
import { streamChat, useApiBase } from '../api';

export type ChatMessage = { role: 'user' | 'agent' | 'system'; text: string };

const CONFIRM_TOKEN_RE = /CONFIRM-[A-F0-9]{4,10}\b/i;

/**
 * One conversation per page, as in the portal.
 *
 * The server keys a conversation by thread id, and a null id makes it start a
 * fresh one per message — so the agent remembered nothing between turns here
 * (onboarding's step-by-step answers, "yes, that one"). The id is kept per
 * page for the browser session, so a reload continues the same conversation.
 */
function threadIdFor(scope: string): string {
  const key = `release-copilot:thread:${scope}`;
  try {
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const id = `backstage-${scope}-${Math.random().toString(36).slice(2, 10)}`;
    window.sessionStorage.setItem(key, id);
    return id;
  } catch {
    return `backstage-${scope}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

export type SendOptions = {
  /** Which tab asked — its result panel shows the reply. Default "chat". */
  origin?: string;
  /** What the chat log shows, when it differs from what is sent. */
  display?: string;
};

/**
 * The shared chat turn: the Chat tab and every form tab send through it.
 * Each reply is also kept per origin, so a form tab can show the reply to
 * ITS submission — the preview, the CONFIRM token, the outcome — right where
 * the person is, instead of sending them to the Chat tab to find it.
 */
export function useAgentChat(scope: string) {
  const apiBase = useApiBase();
  const toastApi = useApi(toastApiRef);
  const [threadId] = useState(() => threadIdFor(scope));
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyOrigin, setBusyOrigin] = useState<string | null>(null);
  const [replies, setReplies] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<{ token: string; origin: string } | null>(null);
  const busyRef = useRef(false);

  const append = useCallback((msg: ChatMessage) => {
    setMessages(prev => [...prev, msg]);
  }, []);

  const send = useCallback(
    async (text: string, opts: SendOptions = {}) => {
      if (busyRef.current) return;
      const origin = opts.origin ?? 'chat';
      busyRef.current = true;
      setBusy(true);
      setBusyOrigin(origin);
      setPending(null);
      setReplies(prev => ({ ...prev, [origin]: '' }));
      append({ role: 'user', text: opts.display ?? text });
      append({ role: 'agent', text: '' });
      let reply = '';
      try {
        await streamChat(apiBase, text, threadId, ev => {
          if (ev.type === 'token' && ev.content) {
            reply += ev.content;
            const current = reply;
            setMessages(prev => {
              const next = [...prev];
              next[next.length - 1] = { ...next[next.length - 1], text: current };
              return next;
            });
            setReplies(prev => ({ ...prev, [origin]: current }));
          } else if (ev.type === 'interrupt') {
            append({
              role: 'system',
              text: '⚠ Confirmation required — reply with the exact CONFIRM token to proceed.',
            });
          } else if (ev.type === 'error') {
            append({ role: 'system', text: `Error: ${ev.content ?? 'unknown'}` });
            toastApi.post({
              title: 'Release Copilot error',
              description: String(ev.content ?? 'unknown error'),
              status: 'danger',
            });
          }
        });
        // A reply that ends asking for a CONFIRM token can be confirmed from
        // the tab that asked for it.
        const match = reply.match(CONFIRM_TOKEN_RE);
        if (match) setPending({ token: match[0].toUpperCase(), origin });
      } catch (e) {
        append({ role: 'system', text: `Error: ${(e as Error).message}` });
        toastApi.post({
          title: 'Release Copilot request failed',
          description: (e as Error).message,
          status: 'danger',
        });
      } finally {
        busyRef.current = false;
        setBusy(false);
        setBusyOrigin(null);
      }
    },
    [apiBase, threadId, append, toastApi],
  );

  /** Send the pending token; the outcome lands where the preview was. */
  const confirm = useCallback(() => {
    if (pending) void send(pending.token, { origin: pending.origin });
  }, [pending, send]);

  const dismiss = useCallback(() => {
    setPending(null);
    append({ role: 'system', text: '✖ Preview dismissed — nothing was deployed.' });
    toastApi.post({
      title: 'Not confirmed',
      description: 'The preview was dismissed — nothing was deployed.',
      status: 'info',
      timeout: 5000,
    });
  }, [append, toastApi]);

  return {
    messages,
    busy,
    send,
    confirm,
    dismiss,
    pending,
    /** The reply to the latest turn a tab started, and whether it is still streaming. */
    resultFor: (origin: string) => ({
      text: replies[origin] ?? '',
      streaming: busyOrigin === origin,
      pendingToken: pending?.origin === origin ? pending.token : null,
    }),
  };
}
