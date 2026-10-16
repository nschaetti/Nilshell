import { spawnSync } from 'node:child_process';
import { mkdirSync, copyFileSync } from 'node:fs';
import { resolve } from 'node:path';

function run(program, args, cwd = process.cwd()) {
  const result = spawnSync(program, args, { cwd, stdio: 'inherit', shell: false });
  if (result.error || result.status !== 0) {
    console.error(result.error?.message ?? `${program} failed`);
    process.exit(1);
  }
}
const rust = spawnSync('rustc', ['-vV'], { encoding: 'utf8' });
const target = process.env.TAURI_ENV_TARGET_TRIPLE ?? rust.stdout?.match(/^host: (.+)$/m)?.[1];
if (!target) throw new Error('Install Rust before building the desktop app.');
const engine = resolve('engine');
run('uv', ['sync', '--frozen'], engine);
run('uv', ['run', '--frozen', 'pyinstaller', '--noconfirm', '--clean', '--onefile', '--name', 'nilshell-engine', '--collect-all', 'keyring', '--hidden-import', 'keyring.backends.SecretService', '--hidden-import', 'keyring.backends.macOS', '--hidden-import', 'keyring.backends.Windows', 'main.py'], engine);
const extension = process.platform === 'win32' ? '.exe' : '';
mkdirSync('src-tauri/binaries', { recursive: true });
copyFileSync(resolve(engine, `dist/nilshell-engine${extension}`), `src-tauri/binaries/nilshell-engine-${target}${extension}`);
