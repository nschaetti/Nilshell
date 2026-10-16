<script lang="ts">
  import { onMount } from 'svelte';
  import { desktop, rpc } from './engine';
  import type { PythonTool } from './types';
  const sampleCode = 'def run(arguments, system):\n    """List files through the Nilshell kernel."""\n    return system.call("fs.list", path=arguments["path"], limit=100)\n';
  const sampleSchema = { type: 'object', properties: { path: { type: 'string', description: 'Absolute directory path, or ~ for the home directory' } }, required: ['path'], additionalProperties: false };
  const initial = (): PythonTool => ({ id: '', name: 'list_directory', description: 'List the files and subdirectories at a local path.', code: sampleCode, parameters: sampleSchema, enabled: true });
  let tools = $state<PythonTool[]>([]);
  let form = $state(initial());
  let schema = $state(JSON.stringify(sampleSchema, null, 2));
  let runtime = $state<{ available: boolean; message: string } | null>(null);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  const explain = (e: unknown) => error = e instanceof Error ? e.message : String(e);
  async function refresh() { tools = await rpc<PythonTool[]>('tools.list'); }
  async function checkRuntime() { try { runtime = await rpc('tools.runtime'); } catch (e) { explain(e); } }
  onMount(() => { if (desktop) { refresh().catch(explain); checkRuntime(); } });
  function edit(tool?: PythonTool) { form = tool ? structuredClone($state.snapshot(tool)) : initial(); schema = JSON.stringify(form.parameters, null, 2); error = ''; notice = ''; }
  async function save() {
    busy = true; error = ''; notice = '';
    try {
      const saved = await rpc<PythonTool>('tools.save', { ...form, id: form.id || null, parameters: JSON.parse(schema) });
      await refresh(); edit(saved); notice = 'Outil enregistré. Active-le sur un assistant, puis démarre une nouvelle conversation.';
    } catch (e) { explain(e); } finally { busy = false; }
  }
  async function remove(tool: PythonTool) {
    if (!confirm(`Supprimer l’outil ${tool.name} ?`)) return;
    busy = true; error = '';
    try { await rpc('tools.delete', { id: tool.id }); await refresh(); if (form.id === tool.id) edit(); }
    catch (e) { explain(e); } finally { busy = false; }
  }
</script>
<section class="settings-page tools-page">
  <div class="eyebrow">PYTHON & APPELS SYSTÈME</div><h1>Outils Python.</h1>
  <p class="subtitle">Écris une fonction run(arguments, system). Pour agir sur ton ordinateur, utilise system.call avec un appel du noyau.</p>
  {#if error}<div class="banner error" role="alert">{error}</div>{/if}
  {#if notice}<div class="banner notice" role="status">{notice}</div>{/if}
  <div class="runtime-status"><span class="status-dot" class:unavailable={runtime && !runtime.available}></span><span>{runtime?.message ?? 'Vérification de l’isolation…'}</span><button class="text-button" onclick={checkRuntime} disabled={!desktop}>Vérifier</button></div>
  <div class="permission-layout"><div class="permission-rules"><button class="outline" onclick={() => edit()}>＋ Nouvel outil (exemple)</button>{#each tools as tool}<article class="permission-card"><button class="rule-title" onclick={() => edit(tool)}><strong>{tool.name}</strong><span>{tool.enabled ? 'Disponible' : 'Désactivé'}</span><small>{tool.description}</small></button><div class="rule-actions"><button onclick={() => remove(tool)} disabled={busy}>Supprimer</button></div></article>{:else}<p class="muted">Le modèle ne voit que les outils activés sur son assistant.</p>{/each}</div>
  <form class="settings-form" onsubmit={event => { event.preventDefault(); save(); }}>
    <label>Nom de fonction<input bind:value={form.name} required pattern={'[A-Za-z_][A-Za-z0-9_]{0,63}'} maxlength="64"/></label>
    {#if form.id}<label>Identifiant pour les permissions<input value={form.id} readonly/></label>{/if}
    <label>Description pour le modèle<textarea bind:value={form.description} required maxlength="5000" rows="3"></textarea></label>
    <label>Paramètres (JSON Schema)<textarea class="json-editor" bind:value={schema} rows="9" spellcheck="false"></textarea></label>
    <label>Code Python<textarea class="json-editor tool-code" bind:value={form.code} rows="12" required maxlength="65536" spellcheck="false"></textarea></label>
    <p class="field-help">Bibliothèque standard Python. Accès à tes fichiers et commandes via le noyau ; pas d’accès réseau direct. Le résultat doit être sérialisable en JSON.</p>
    <label class="checkbox"><input type="checkbox" bind:checked={form.enabled}/> Outil disponible pour les assistants</label>
    <button class="primary" disabled={!desktop || busy}>{busy ? 'Enregistrement…' : 'Enregistrer l’outil'}</button>
  </form></div>
</section>
