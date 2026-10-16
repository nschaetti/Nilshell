export type Provider = { id: string; name: string; kind: 'openai' | 'ollama'; base_url: string; has_key: boolean; warning?: string };
export type Assistant = { id: string; name: string; provider_id: string; model: string; system_prompt: string };
export type Message = { id: number; role: 'user' | 'assistant'; content: string; status: string };
export type Conversation = { id: string; title: string; assistant: Assistant; messages?: Message[] };
export type Bootstrap = { providers: Provider[]; assistants: Assistant[]; conversations: Conversation[] };
export type EngineEvent = { event: string; conversation_id?: string; message_id?: number; delta?: string; status?: string; error?: string };
