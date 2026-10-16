<script lang="ts">
  import { onMount } from 'svelte';
  import { desktop, rpc } from './engine';

  type Definition = { name: string; description: string; schema: Record<string, unknown> };
  type Prepared = { id: string; syscall: string; arguments: Record<string, unknown>; warning: string | null; expires_in_seconds: number };
  type Audit = { id: string; syscall: string; resource: string; state: string; code: string | null; duration_ms: number; created_at: string };
  const examples: Record<string, unknown> = {
    'fs.stat': { path: '~' },
    'fs.list': { path: '~', limit: 100 },
    'fs.read': { path: '~/example.txt', max_bytes: 131072 },
    'fs.write': { path: '~/example.txt', content: 'Bonjour depuis Nilshell.\n', overwrite: false },
    'fs.mkdir': { path: '~/nilshell-example' },
    'process.run': { argv: ['whoami'], cwd: '~', timeout_seconds: 15, max_output_bytes: 65536 }
  };
  let catalog = $state<Definition[]>([]);
  let history = $state<Audit[]>([]);
  let syscall = $state('fs.list');
  let argumentsText = $state(JSON.stringify(examples['fs.list'], null, 2));
  let pending = $state<Prepared | null>(null);
  let result = $state('');
  let error = $state('');
  let working = $state(false);
  const explain = (value: unknown) => { error = value instanceof Error ? value.message : String(value); };

  async function refresh() { history = await rpc<Audit[]>('system.history'); }
  onMount(() => {
    if (desktop) Promise.all([rpc<Definition[]>('system.catalog').then(value => catalog = value), refresh()]).catch(explain);
  });
  async function prepare() {
    working = true; error = ''; result = '';
    try {
      const args = JSON.parse(argumentsText);
      pending = await rpc<Prepared>('system.prepare', { syscall, arguments: args });
      await refresh();
    } catch (e) { explain(e); } finally { working = false; }
  }
  async function decide(approved: boolean) {
    if (!pending || working) return;
    const id = pending.id;
    pending = null; working = true; error = '';
    try {
      const value = await rpc('system.execute', { id, approved });
      result = JSON.stringify(value, null, 2);
    } catch (e) { if (approved) explain(e); else result = 'Appel refusé.'; }
    finally { working = false; await refresh().catch(explain); }
  }
</script>

<section class="settings-page system-page">
  <div class="eyebrow">NOYAU LOCAL</div>
  <h1>Appels système.</h1>
  <p class="subtitle">Teste les opérations du noyau. Chaque demande requiert ton accord pour une seule exécution. Les assistants n’y ont pas encore accès.</p>
  {#if error}<div class="banner error" role="alert">{error}</div>{/if}
  <div class="settings-form">
    <form onsubmit={event => { event.preventDefault(); prepare(); }}>
      <label>Opération<select bind:value={syscall} disabled={working || !!pending} onchange={() => { argumentsText = JSON.stringify(examples[syscall], null, 2); result = ''; }}>
        {#each Object.keys(examples) as name}<option value={name}>{name}</option>{/each}
      </select></label>
      <p class="field-help operation-description">{catalog.find(item => item.name === syscall)?.description ?? 'Démarre l’application desktop pour utiliser le noyau.'}</p>
      <label>Arguments JSON<textarea class="json-editor" bind:value={argumentsText} rows="8" disabled={working || !!pending} spellcheck="false"></textarea></label>
      <button class="primary" disabled={!desktop || working || !!pending}>{working ? 'Traitement…' : 'Préparer l’appel'}</button>
    </form>
    {#if pending}
      <section class="approval-box" aria-label="Demande d’autorisation">
        <h2>Autoriser {pending.syscall} ?</h2>
        <p>Vérifie les chemins résolus et les arguments. Cette demande expire après cinq minutes.</p>
        <pre>{JSON.stringify(pending.arguments, null, 2)}</pre>
        {#if pending.warning}<p class="command-warning">{pending.warning}</p>{/if}
        <div class="approval-actions"><button class="outline" onclick={() => decide(false)}>Refuser</button><button class="primary" onclick={() => decide(true)}>Autoriser une fois et exécuter</button></div>
      </section>
    {/if}
    {#if result}<section aria-label="Résultat"><h2>Résultat</h2><pre class="system-result">{result}</pre></section>{/if}
  </div>
  <div class="audit-heading"><h2>Journal des appels</h2><button class="text-button" onclick={() => refresh().catch(explain)} disabled={!desktop}>Actualiser</button></div>
  <p class="field-help">Les contenus lus ou écrits, les arguments de commande et les sorties ne sont pas enregistrés dans ce journal.</p>
  <div class="audit-list">{#each history as item}<article><div><strong>{item.syscall}</strong><span>{item.state}{item.code ? ` · ${item.code}` : ''}</span></div><code>{item.resource}</code><small>{new Date(item.created_at).toLocaleString()} · {item.duration_ms ?? 0} ms</small></article>{:else}<p class="muted">Aucun appel pour le moment.</p>{/each}</div>
</section>
