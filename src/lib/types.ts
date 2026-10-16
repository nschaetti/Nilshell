export type Provider = { id: string; name: string; kind: 'openai' | 'ollama'; base_url: string; has_key: boolean; warning?: string };
export type Assistant = { id: string; name: string; provider_id: string; model: string; system_prompt: string; tool_ids?: string[] };
export type Message = { id: number; role: 'user' | 'assistant'; content: string; status: string };
export type Conversation = { id: string; title: string; assistant: Assistant; messages?: Message[]; tool_runs?: ToolRun[] };
export type Bootstrap = { providers: Provider[]; assistants: Assistant[]; conversations: Conversation[]; tools: PythonTool[] };
export type EngineEvent = { event: string; conversation_id?: string; message_id?: number; delta?: string; status?: string; error?: string; request?: ToolApproval; request_id?: string; run_id?: string; name?: string; tool_id?: string; arguments?: unknown; result?: unknown; state?: string };

export type PythonTool = { id: string; name: string; description: string; code: string; parameters: Record<string, unknown>; enabled: boolean };
export type ToolRun = { id: string; name: string; tool_id: string; arguments: string; result: string | null; state: string; message_id?: number };
export type ToolApproval = { id: string; syscall: string; arguments: Record<string, unknown>; caller: { tool_id: string; model: string }; warning: string | null };
