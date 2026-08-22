/** Client helpers for the release-copilot service via the Backstage proxy. */

import { useApi, configApiRef } from '@backstage/core-plugin-api';

/**
 * The proxy lives on the Backstage BACKEND (dev: :7007, container: :7007), not
 * the frontend dev server (:3000) — so all calls must be prefixed with the
 * backend baseUrl from config.
 */
export function useApiBase(): string {
  const config = useApi(configApiRef);
  return `${config.getString('backend.baseUrl')}/api/proxy/release-copilot`;
}

export type ChatEvent = {
  type: 'token' | 'progress' | 'interrupt' | 'confirmation' | 'done' | 'error';
  content?: string;
  data?: unknown;
  mutated?: boolean;
};

export async function apiGet<T>(base: string, path: string): Promise<T> {
  const resp = await fetch(`${base}${path}`);
  if (!resp.ok) throw new Error(`GET ${path}: HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

export async function apiPost<T>(base: string, path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await resp.json().catch(() => ({}))) as T & {
    ok?: boolean;
    error?: string;
  };
  if (!resp.ok) throw new Error(`POST ${path}: HTTP ${resp.status}`);
  if (data && data.ok === false) {
    throw new Error(data.error || `POST ${path} failed`);
  }
  return data;
}

/** Stream one chat turn through the SSE proxy, emitting parsed events. */
export async function streamChat(
  base: string,
  message: string,
  threadId: string | null,
  onEvent: (ev: ChatEvent) => void,
): Promise<void> {
  const resp = await fetch(`${base}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, thread_id: threadId }),
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`chat failed: HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith('data:')) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
      } catch {
        /* ignore malformed frame */
      }
    }
  }
}
