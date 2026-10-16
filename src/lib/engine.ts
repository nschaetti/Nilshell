import { invoke, isTauri } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import type { EngineEvent } from './types';

type Reply = { id?: string; result?: unknown; error?: string } & Partial<EngineEvent>;
const pending = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
const listeners = new Set<(event: EngineEvent) => void>();
let ready: Promise<unknown> | undefined;
export const desktop = isTauri();

function initialize() {
  if (!desktop) return Promise.reject(new Error('Ouvre Nilshell avec npm run desktop dev pour utiliser le moteur local.'));
  return ready ??= listen<Reply>('engine', ({ payload }) => {
    if (payload.event === 'engine.stopped') {
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('Le moteur local s’est arrêté. Relance Nilshell.')); }
      pending.clear();
    }
    if (payload.event) { for (const listener of listeners) listener(payload as EngineEvent); }
    else if (payload.id) {
      const item = pending.get(payload.id);
      if (item) {
        clearTimeout(item.timer); pending.delete(payload.id);
        if (payload.error) item.reject(new Error(payload.error)); else item.resolve(payload.result);
      }
    }
  });
}
export async function rpc<T>(method: string, params: unknown = {}): Promise<T> {
  await initialize();
  const id = crypto.randomUUID();
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Le moteur ne répond pas.')); }, method === 'system.execute' ? 75000 : 45000);
    pending.set(id, { resolve: value => resolve(value as T), reject, timer });
    invoke('engine_send', { request: { id, method, params } }).catch(error => {
      clearTimeout(timer); pending.delete(id); reject(new Error(String(error)));
    });
  });
}
export function onEngineEvent(callback: (event: EngineEvent) => void) {
  listeners.add(callback);
  return () => listeners.delete(callback);
}
