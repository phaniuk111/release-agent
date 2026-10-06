import { useCallback, useRef, useState } from 'react';
import { toastApiRef } from '@backstage/frontend-plugin-api';
import { useApi } from '@backstage/core-plugin-api';
import { streamChat, useApiBase } from '../api';

export type ChatMessage = { role: 'user' | 'agent' | 'system'; text: string };

const CONFIRM_TOKEN_RE = /CONFIRM-[A-F0-9]{4,10}\b/i;

/** A paused yes/no approval (a terminal promotion): the agent's own question. */
export type Approval = { message: string; origin: string };

/** What an Investigate answer reports when it is complete (feedback needs it). */
export type Investigation = {
  business_date: string;
  incident_id: string;
  title?: string;
  model?: string;
  model_calls?: number;
  seconds?: number;
  shared?: boolean;
};

/**
 * The agent pauses in two ways, never mixed (as the portal's chat.js tells
 * them apart): a deploy/release preview carries an exact CONFIRM token; a
 * high-impact operation asks yes/no — no token, it names the function or says
 * 'Reply "yes"'. Pasting a token into a yes/no would reject it.
 */
export function interruptKind(data: unknown): { token: string } | { approval: string } | null {
  const d = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>;
  const token = typeof d.token === 'string' ? d.token : '';
  if (token) return { token: token.toUpperCase() };
  const said = `${d.action ?? ''} ${d.message ?? ''}`.toLowerCase();
  if (d.type !== 'budget_confirmation' && (d.function || said.includes('"yes"'))) {
    return { approval: String(d.message || 'Approve this operation?') };
  }
  return null;
}

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
  /** This turn answers a preview or an approval: going ahead, or not. */
  answer?: 'go' | 'stop';
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
  const [approval, setApproval] = useState<Approval | null>(null);
  const [answering, setAnswering] = useState<'go' | 'stop' | null>(null);
  const [progress, setProgress] = useState<Record<string, string[]>>({});
  const [investigations, setInvestigations] = useState<Record<string, Investigation | null>>({});
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
      setAnswering(opts.answer ?? null);
      setPending(null);
      setApproval(null);
      setReplies(prev => ({ ...prev, [origin]: '' }));
      setProgress(prev => ({ ...prev, [origin]: [] }));
      setInvestigations(prev => ({ ...prev, [origin]: null }));
      let tokenFromAgent: string | null = null;
      let approvalSeen = false;
      // The Ask chat log holds the conversation only. A form's submission and
      // its preview stay in the Ship / Queue screen that sent it; a preview the
      // chat itself raised is confirmed there too, never in Ask.
      const inLog = origin === 'chat';
      if (inLog) {
        append({ role: 'user', text: opts.display ?? text });
        append({ role: 'agent', text: '' });
      }
      let reply = '';
      try {
        await streamChat(apiBase, text, threadId, ev => {
          if (ev.type === 'token' && ev.content) {
            reply += ev.content;
            const current = reply;
            if (inLog) {
              setMessages(prev => {
                const next = [...prev];
                next[next.length - 1] = { ...next[next.length - 1], text: current };
                return next;
              });
            }
            setReplies(prev => ({ ...prev, [origin]: current }));
          } else if (ev.type === 'progress' && ev.content) {
            const label = ev.content;
            setProgress(prev => ({ ...prev, [origin]: [...(prev[origin] ?? []), label] }));
          } else if (ev.type === 'interrupt') {
            const kind = interruptKind(ev.data);
            if (kind && 'approval' in kind) {
              setApproval({ message: kind.approval, origin });
              approvalSeen = true;
            } else if (kind && 'token' in kind) {
              tokenFromAgent = kind.token;
            }
          } else if (ev.type === 'investigation' && ev.data && typeof ev.data === 'object') {
            const data = ev.data as Investigation;
            setInvestigations(prev => ({ ...prev, [origin]: data }));
          } else if (ev.type === 'error') {
            if (inLog) append({ role: 'system', text: `Error: ${ev.content ?? 'unknown'}` });
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
        const token = tokenFromAgent ?? (match ? match[0].toUpperCase() : null);
        if (token) setPending({ token, origin });
        if (inLog && (token || approvalSeen)) {
          // The preview is shown — and answered — in Ship; Ask only says so.
          const pointer = token
            ? 'Preview ready — review it and confirm or cancel it in **Ship**.'
            : 'Approval waiting — answer it in **Ship**.';
          setMessages(prev => {
            const next = [...prev];
            next[next.length - 1] = { role: 'agent', text: pointer };
            return next;
          });
        }
      } catch (e) {
        if (inLog) append({ role: 'system', text: `Error: ${(e as Error).message}` });
        toastApi.post({
          title: 'Release Copilot request failed',
          description: (e as Error).message,
          status: 'danger',
        });
      } finally {
        busyRef.current = false;
        setBusy(false);
        setBusyOrigin(null);
        setAnswering(null);
      }
    },
    [apiBase, threadId, append, toastApi],
  );

  /** Send the pending token; the outcome lands where the preview was. */
  const confirm = useCallback(() => {
    if (pending) void send(pending.token, { origin: pending.origin, answer: 'go' });
  }, [pending, send]);

  // Cancelling a preview answers it: the agent holds a pending token until it
  // hears the token or "no" — anything else is only a reminder that it waits —
  // so a cancel that stayed on this page would leave it waiting.
  const dismiss = useCallback(() => {
    const origin = pending?.origin ?? 'chat';
    setPending(null);
    void send('no', { origin, display: 'no', answer: 'stop' });
    toastApi.post({
      title: 'Not confirmed',
      description: 'The preview was dismissed — nothing was deployed.',
      status: 'info',
      timeout: 5000,
    });
  }, [append, toastApi, pending, send]);

  /** Answer a paused yes/no approval; the outcome lands in the tab that asked. */
  const answerApproval = useCallback(
    (yes: boolean) => {
      if (!approval) return;
      const { origin } = approval;
      setApproval(null);
      void send(yes ? 'yes' : 'no', { origin, answer: yes ? 'go' : 'stop' });
    },
    [approval, send],
  );

  return {
    messages,
    busy,
    send,
    confirm,
    dismiss,
    pending,
    approval,
    /** The turn in flight answers a preview or approval: 'go' carries it out, 'stop' cancels it. */
    answering,
    approve: () => answerApproval(true),
    reject: () => answerApproval(false),
    /** The reply to the latest turn a tab started, and whether it is still streaming. */
    resultFor: (origin: string) => ({
      text: replies[origin] ?? '',
      streaming: busyOrigin === origin,
      pendingToken: pending?.origin === origin ? pending.token : null,
      progress: progress[origin] ?? [],
    }),
    /** The complete Investigate answer a tab got, for its "Was this right?". */
    investigationFor: (origin: string) => investigations[origin] ?? null,
  };
}
