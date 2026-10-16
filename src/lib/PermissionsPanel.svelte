<script lang="ts">
  import { onMount } from 'svelte';
  import { desktop, rpc } from './engine';
  import type { Assistant, Bootstrap, Provider } from './types';
  type Rule = { id: string | null; name: string; effect: 'allow' | 'deny' | 'ask'; scope: 'session' | 'permanent'; enabled: boolean; resource_hint: string | null; assistant_id: string | null; provider_id: string | null; model: string | null; tool_id: string | null; syscall: string | null; directory: string | null; recursive: boolean; command: string | null; arguments: string[] | null; argument_prefix: string[] | null; parameters: Record<string, unknown>; call_digest: string | null };
  const initial = (): Rule => ({ id: null, name: '', effect: 'ask', scope: 'permanent', enabled: true, resource_hint: null, assistant_id: null, provider_id: null, model: null, tool_id: null, syscall: null, directory: null, recursive: true, command: null, arguments: null, argument_prefix: null, parameters: {}, call_digest: null });
  let rules = $state<Rule[]>([]);
  let assistants = $state<Assistant[]>([]);
  let providers = $state<Provider[]>([]);
  let catalog = $state<{ name: string }[]>([]);
  let form = $state<Rule>(initial());
  let parameters = $state('{}');
  let argumentsText = $state('');
  let prefixText = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  const conditionExample = '{"max_bytes":{"op":"lte","value":131072}}';
  const explain = (e: unknown) => error = e instanceof Error ? e.message : String(e);
  async function refresh() { rules = await rpc<Rule[]>('permissions.list'); }
  onMount(() => {
    if (desktop) Promise.all([refresh(), rpc<Bootstrap>('bootstrap').then(data => { assistants = data.assistants; providers = data.providers; }), rpc<{ name: string }[]>('system.catalog').then(data => catalog = data)]).catch(explain);
  });
  function edit(rule?: Rule) {
    form = rule ? structuredClone($state.snapshot(rule)) : initial();
    parameters = JSON.stringify(form.parameters, null, 2);
    argumentsText = form.arguments === null ? '' : JSON.stringify(form.arguments);
    prefixText = form.argument_prefix === null ? '' : JSON.stringify(form.argument_prefix);
    notice = ''; error = '';
  }
  async function save() {
    busy = true; error = ''; notice = '';
    try {
      const data = { ...form, parameters: JSON.parse(parameters), arguments: argumentsText.trim() ? JSON.parse(argumentsText) : null, argument_prefix: prefixText.trim() ? JSON.parse(prefixText) : null };
      for (const key of ['assistant_id', 'provider_id', 'model', 'tool_id', 'syscall', 'directory', 'command'] as const) data[key] = data[key] || null;
      const saved = await rpc<Rule>('permissions.save', data);
      await refresh(); edit(saved); notice = 'Règle enregistrée. Elle sera vérifiée au prochain appel.';
    } catch (e) { explain(e); } finally { busy = false; }
  }
  async function toggle(rule: Rule) {
    busy = true; error = '';
    try { await rpc('permissions.save', { ...rule, enabled: !rule.enabled }); await refresh(); if (form.id === rule.id) edit(rules.find(r => r.id === rule.id)); }
    catch (e) { explain(e); } finally { busy = false; }
  }
  async function remove(rule: Rule) {
    busy = true; error = '';
    try { await rpc('permissions.delete', { id: rule.id }); await refresh(); if (form.id === rule.id) edit(); }
    catch (e) { explain(e); } finally { busy = false; }
  }
</script>

<section class="settings-page permission-page">
  <div class="eyebrow">CONTRÔLE DES ACCÈS</div><h1>Permissions.</h1>
  <p class="subtitle">Tous les critères d’une règle doivent correspondre. Priorité : refuser → demander → autoriser. Sans règle, Nilshell demande confirmation.</p>
  {#if error}<div class="banner error" role="alert">{error}</div>{/if}
  {#if notice}<div class="banner notice" role="status">{notice}</div>{/if}
  <div class="permission-layout">
    <div class="permission-rules"><button class="outline" onclick={() => edit()} disabled={busy}>＋ Nouvelle règle</button>
      {#each rules as rule}<article class:disabled-rule={!rule.enabled} class="permission-card"><button class="rule-title" onclick={() => edit(rule)}><strong>{rule.name}</strong><span>{rule.effect === 'deny' ? 'Refuser' : rule.effect === 'ask' ? 'Demander' : 'Autoriser'} · {rule.scope === 'session' ? 'Session' : 'Permanent'}</span><code>{rule.syscall ?? 'Tous les appels'}</code><small>{rule.directory ?? rule.command ?? rule.resource_hint ?? (rule.call_digest ? 'Appel exact · ' + rule.call_digest.slice(0, 10) : 'Critères personnalisés')}</small></button><div class="rule-actions"><button onclick={() => toggle(rule)} disabled={busy}>{rule.enabled ? 'Désactiver' : 'Activer'}</button><button onclick={() => remove(rule)} disabled={busy}>Révoquer</button></div></article>{:else}<p class="muted">Aucune règle. Les appels demandent confirmation.</p>{/each}
    </div>
    <form class="settings-form permission-form" onsubmit={event => { event.preventDefault(); save(); }}>
      <h2>{form.id ? 'Modifier la règle' : 'Nouvelle règle'}</h2>
      <label>Nom<input bind:value={form.name} maxlength="150" required placeholder="Ada peut lire mes articles"/></label>
      <div class="form-pair"><label>Décision<select bind:value={form.effect}><option value="ask">Demander</option><option value="allow">Autoriser</option><option value="deny">Refuser</option></select></label><label>Durée<select bind:value={form.scope}><option value="permanent">Permanente</option><option value="session">Jusqu’à fermeture de Nilshell</option></select></label></div>
      <label>Assistant<select bind:value={form.assistant_id}><option value={null}>Tous les assistants et la console</option>{#each assistants as assistant}<option value={assistant.id}>{assistant.name}</option>{/each}</select></label>
      <div class="form-pair"><label>Fournisseur<select bind:value={form.provider_id}><option value={null}>Tous les fournisseurs</option>{#each providers as provider}<option value={provider.id}>{provider.name}</option>{/each}</select></label><label>Modèle exact<input bind:value={form.model} placeholder="Tous les modèles"/></label></div>
      <label>Identifiant de l’outil<input bind:value={form.tool_id} placeholder="Tous les outils (console : desktop.console)"/></label>
      <label>Appel système<select bind:value={form.syscall}><option value={null}>Tous les appels</option>{#each catalog as item}<option value={item.name}>{item.name}</option>{/each}</select></label>
      <label>Répertoire<input bind:value={form.directory} placeholder="Optionnel : ~/Documents/Recherche"/></label>
      <label class="checkbox"><input type="checkbox" bind:checked={form.recursive}/> Inclure tous les sous-répertoires</label>
      <p class="field-help">Sans récursion : le dossier et ses entrées directes. Pour une commande, ce critère filtre uniquement le dossier de travail.</p>
      <label>Programme<input bind:value={form.command} placeholder="Optionnel : git ou /usr/bin/git"/></label>
      <label>Arguments exacts, sans le programme (JSON)<input bind:value={argumentsText} placeholder='Vide = tous ; [] = aucun argument ; ["status"]'/></label>
      <label>Préfixe des arguments (JSON)<input bind:value={prefixText} placeholder='Optionnel : ["log", "--oneline"]'/></label>
      <label>Conditions sur les paramètres (JSON)<textarea class="json-editor" bind:value={parameters} rows="5" spellcheck="false"></textarea></label>
      <p class="field-help">Exemple : <code>{conditionExample}</code>. Opérateurs : eq, in, lte, gte, prefix. Les conditions se combinent par ET.</p>
      {#if form.call_digest}<p class="field-help">Cette règle mémorise un appel exact : {form.resource_hint ?? form.call_digest.slice(0, 10)}. Son empreinte reste active pendant l’édition ; crée une nouvelle règle pour élargir les droits.</p>{/if}
      <label class="checkbox"><input type="checkbox" bind:checked={form.enabled}/> Règle active</label>
      <p class="field-help">Un critère vide accepte toutes ses valeurs. Une règle « Autoriser » sans critères autorise tous les appels du noyau.</p>
      <button class="primary" disabled={!desktop || busy}>{busy ? 'Enregistrement…' : 'Enregistrer la règle'}</button>
    </form>
  </div>
</section>
