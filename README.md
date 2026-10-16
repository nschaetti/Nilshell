# Nilshell

A desktop workspace for AI assistants. Linux first, with a Tauri foundation for macOS and Windows.

## Desktop 0.1

- OpenAI-compatible API providers (OpenAI, OpenRouter, custom endpoints) and native Ollama.
- Text conversations with streaming, stop generation, sanitized Markdown and local history.
- Assistants with a name, provider, model and system prompt.
- Model discovery or manual model identifiers.
- API keys in the system keyring; session-only fallback if no keyring is available.
- Opt-in desktop notifications when a response finishes while the app is unfocused.

Python tools, agent permissions, decision models, attachments and knowledge indexing are planned next. This version does not execute agent tools.

## Run on Linux

Requirements: Node.js 22+, [Rust](https://www.rust-lang.org/tools/install) stable and [uv](https://docs.astral.sh/uv/getting-started/installation/) with Python 3.11+ available. `uv` creates the Python environment and builds the embedded engine automatically.

On Ubuntu / Debian, install the Tauri system dependencies:

```bash
sudo apt update
sudo apt install build-essential pkg-config libwebkit2gtk-4.1-dev libxdo-dev libssl-dev librsvg2-dev libayatana-appindicator3-dev patchelf
```

Then:

```bash
git clone https://github.com/nschaetti/Nilshell.git
cd Nilshell
npm ci
npm run desktop dev
```

The first launch packages the Python engine with PyInstaller and compiles Tauri. No Docker or separately running API service is required. A working Secret Service keyring (such as GNOME Keyring) allows keys to survive application restarts.

1. Open **Fournisseurs**, select a preset, enter the URL and optional API key, then save.
2. Open **Assistants**, select the provider, load or enter a model, add a system prompt, then save.
3. Start a conversation. Enter sends; Shift+Enter inserts a newline.

For OpenAI-compatible endpoints, the base URL includes `/v1` when required. For Ollama, use the server root, e.g. `http://localhost:11434`, without `/api`. If Ollama is remote, use its reachable address. Anthropic models are available through an OpenAI-compatible provider such as OpenRouter; the native Anthropic API is not yet implemented.

## Build an installer

```bash
npm run desktop build
```

Installers are generated in `src-tauri/target/release/bundle/`. The packaged app includes Python and the engine dependencies. Build on each target OS; cross-compilation of the Python sidecar is not supported. macOS and Windows support is a target of the architecture, not yet manually validated. See [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for their toolchains. Production distribution also requires platform signing/notarization as appropriate.

## Development checks

```bash
npm run check
npm run build
cd engine
uv sync --frozen
uv run pytest
uv run ruff check .
```

`npm run dev` alone opens a visual preview without an engine; real requests require `npm run desktop dev`.

Data is stored in Tauri's per-user app-data directory for `ch.nschaetti.nilshell` (typically `$XDG_DATA_HOME/ch.nschaetti.nilshell` or `~/.local/share/ch.nschaetti.nilshell` on Linux). The SQLite file contains chat text and prompts, unencrypted. API keys are excluded. Existing conversations keep a snapshot of their assistant instructions.

See [architecture and upcoming system-call model](docs/architecture.md) for boundaries and the roadmap.

## License

GPL-3.0; see [LICENSE](LICENSE).
