/** Client helpers for the release-copilot service via the Backstage proxy. */

import { useEffect, useState } from 'react';
import { useApi, configApiRef, identityApiRef } from '@backstage/core-plugin-api';

/**
 * The proxy lives on the Backstage BACKEND (dev: :7007, container: :7007), not
 * the frontend dev server (:3000) — so all calls must be prefixed with the
 * backend baseUrl from config.
 */
export function useApiBase(): string {
  const config = useApi(configApiRef);
  return `${config.getString('backend.baseUrl')}/api/proxy/release-copilot`;
}

/** What the agent says this caller's view needs — the portal's window.PORTAL_UI. */
export type UiConfig = {
  /** A model is on: Investigate, Ask why and drafting are offered. */
  llm: boolean;
  /** Pill groups this caller does not see (preview features they are not in). */
  hiddenGroups: string[];
  previewGroups: string[];
  preview: boolean;
};

const UI_DEFAULTS: UiConfig = { llm: true, hiddenGroups: [], previewGroups: [], preview: false };

/**
 * /api/ui-config, read once per page. Until it answers (or if it cannot), the
 * page renders as if a model is on and nothing is preview — the agent refuses a
 * preview feature server-side anyway, so a wrong guess shows a refusal, never
 * data the caller may not see.
 */
export function useUiConfig(): UiConfig {
  const base = useApiBase();
  const [ui, setUi] = useState<UiConfig>(UI_DEFAULTS);
  useEffect(() => {
    let live = true;
    apiGet<Partial<UiConfig>>(base, '/api/ui-config')
      .then(got => {
        if (live) setUi({ ...UI_DEFAULTS, ...got });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [base]);
  return ui;
}

/**
 * Who Backstage says is signed in — behind the mesh, the person in the verified
 * RCToken (the `rctoken` provider); in local development, the guest. Shown in
 * the page header so nobody has to open Settings to see it.
 */
export function useSignedInAs(): string | null {
  const identityApi = useApi(identityApiRef);
  const [who, setWho] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    Promise.all([identityApi.getProfileInfo(), identityApi.getBackstageIdentity()])
      .then(([profile, identity]) => {
        if (!live) return;
        const guest = identity.userEntityRef.endsWith('/guest');
        setWho(guest ? 'Guest' : profile.displayName || profile.email || identity.userEntityRef);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [identityApi]);
  return who;
}

export type ChatEvent = {
  type: 'token' | 'progress' | 'interrupt' | 'investigation' | 'confirmation' | 'done' | 'error';
  content?: string;
  data?: unknown;
  mutated?: boolean;
};

export async function apiGet<T>(base: string, path: string): Promise<T> {
  const resp = await fetch(`${base}${path}`);
  if (!resp.ok) throw new Error(`GET ${path}: HTTP ${resp.status}`);
  return (await resp.json()) as T;
}

export async function apiPost<T>(
  base: string,
  path: string,
  body: unknown,
  opts?: { allowFailure?: boolean },
): Promise<T> {
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
  if (data && data.ok === false && !opts?.allowFailure) {
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
