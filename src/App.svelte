<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { marked } from 'marked';
  import DOMPurify from 'dompurify';
  import { isPermissionGranted, requestPermission, sendNotification } from '@tauri-apps/plugin-notification';
  import { desktop, rpc, onEngineEvent } from './lib/engine';
  import SystemPanel from './lib/SystemPanel.svelte';
  import type { Assistant, Bootstrap, Conversation, EngineEvent, Provider } from './lib/types';

  let providers = $state<Provider[]>([]);
  let assistants = $state<Assistant[]>([]);
  let conversations = $state<Conversation[]>([]);
  let current = $state<Conversation | null>(null);
  let selectedAssistant = $state('');
  let view = $state<'chat' | 'providers' | 'assistants' | 'system'>('chat');
  let input = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let saving = $state(false);
  let loadingModels = $state(false);
  let activeConversation = $state<string | null>(null);
  let models = $state<string[]>([]);
  let scroller = $state<HTMLDivElement>();
  let providerForm = $state({ id: '', name: '', kind: 'openai' as 'openai' | 'ollama', base_url: 'https://api.openai.com/v1', api_key: '', clear_key: false });
  let assistantForm = $state({ id: '', name: '', provider_id: '', model: '', system_prompt: '' });
  let notifications = $state(false);
  let notificationPreference = false;
  const markdown = (text: string) => DOMPurify.sanitize(marked.parse(text, { async: false }) as string, { FORBID_TAGS: ['img'], FORBID_ATTR: ['style'] });
  const explain = (e: unknown) => { error = e instanceof Error ? e.message : String(e); };

  async function refresh() {
    const state = await rpc<Bootstrap>('bootstrap');
    providers = state.providers; assistants = state.assistants; conversations = state.conversations;
    if (!selectedAssistant || !assistants.some(a => a.id === selectedAssistant)) selectedAssistant = assistants[0]?.id ?? '';
  }
  onMount(() => {
    notificationPreference = localStorage.getItem('nilshell.notifications') === 'true';
    if (desktop) {
      isPermissionGranted().then(granted => { notifications = granted && notificationPreference; }).catch(() => {});
      refresh().catch(explain);
    }
    return onEngineEvent(handleEvent);
  });
  async function scroll() { await tick(); scroller?.scrollTo({ top: scroller.scrollHeight }); }
  async function handleEvent(event: EngineEvent) {
    if (event.event === 'engine.stopped') { busy = false; error = 'Le moteur local s’est arrêté. Relance Nilshell.'; return; }
    if (event.conversation_id !== activeConversation) return;
    if (event.event === 'chat.delta' && current?.id === event.conversation_id) {
      let message = current.messages?.find(m => m.id === event.message_id);
      if (!message) {
        message = { id: event.message_id!, role: 'assistant', content: '', status: 'streaming' };
        current.messages = [...(current.messages ?? []), message];
      }
      current.messages = current.messages!.map(m => m.id === event.message_id ? { ...m, content: m.content + event.delta } : m);
      await scroll();
    }
    if (event.event === 'chat.done') {
      busy = false; activeConversation = null;
      if (event.error) error = event.error;
      try {
        if (current?.id === event.conversation_id) current = await rpc<Conversation>('conversation.get', { id: event.conversation_id });
        await refresh();
      } catch (e) { explain(e); }
      if (notifications && !document.hasFocus()) {
        sendNotification({ title: 'Nilshell', body: event.status === 'complete' ? 'La réponse est prête.' : 'La génération s’est arrêtée.' });
      }
      await scroll();
    }
  }
  async function openConversation(id: string) {
    if (busy) return;
    error = '';
    try { current = await rpc<Conversation>('conversation.get', { id }); selectedAssistant = current.assistant.id; view = 'chat'; await scroll(); } catch (e) { explain(e); }
  }
  function newChat() { if (busy) return; current = null; input = ''; error = ''; view = 'chat'; }
  async function send() {
    if (busy || !input.trim() || !selectedAssistant) return;
    error = ''; busy = true;
    const content = input.trim();
    try {
      if (!current) current = await rpc<Conversation>('conversation.create', { assistant_id: selectedAssistant });
      activeConversation = current.id;
      current.messages = [...(current.messages ?? []), { id: -1, role: 'user', content, status: 'complete' }];
      input = '';
      await rpc('chat.start', { id: current.id, content });
      await scroll();
    } catch (e) {
      busy = false; activeConversation = null; input = content; explain(e);
      if (current) { try { current = await rpc<Conversation>('conversation.get', { id: current.id }); } catch { /* Keep the visible draft. */ } }
    }
  }
  async function cancel() {
    if (!activeConversation) return;
    try { await rpc('chat.cancel', { id: activeConversation }); notice = 'Arrêt demandé…'; } catch (e) { explain(e); }
  }
  async function removeConversation() {
    if (!current || busy || !confirm('Supprimer cette conversation ?')) return;
    try { await rpc('conversation.delete', { id: current.id }); current = null; await refresh(); } catch (e) { explain(e); }
  }
  async function saveProvider() {
    saving = true; error = ''; notice = '';
    try {
      const { api_key, id, ...rest } = providerForm;
      const result = await rpc<Provider>('provider.save', { ...rest, id: id || null, api_key: api_key || null });
      await refresh(); providerForm = { ...providerForm, id: result.id, api_key: '', clear_key: false };
      notice = result.warning ?? 'Fournisseur enregistré.';
    } catch (e) { explain(e); } finally { saving = false; }
  }
  function editProvider(provider?: Provider) {
    providerForm = { id: provider?.id ?? '', name: provider?.name ?? '', kind: provider?.kind ?? 'openai', base_url: provider?.base_url ?? 'https://api.openai.com/v1', api_key: '', clear_key: false };
    notice = ''; error = '';
  }
  function preset(kind: 'openai' | 'ollama' | 'openrouter') {
    providerForm = { id: '', name: kind === 'ollama' ? 'Ollama' : kind === 'openrouter' ? 'OpenRouter' : 'OpenAI', kind: kind === 'ollama' ? 'ollama' : 'openai', base_url: kind === 'ollama' ? 'http://localhost:11434' : kind === 'openrouter' ? 'https://openrouter.ai/api/v1' : 'https://api.openai.com/v1', api_key: '', clear_key: false };
  }
  function editAssistant(assistant?: Assistant) {
    assistantForm = { id: assistant?.id ?? '', name: assistant?.name ?? '', provider_id: assistant?.provider_id ?? providers[0]?.id ?? '', model: assistant?.model ?? '', system_prompt: assistant?.system_prompt ?? '' };
    models = []; notice = ''; error = '';
  }
  async function loadModels() {
    if (!assistantForm.provider_id) return;
    loadingModels = true; error = '';
    try { models = await rpc<string[]>('provider.models', { id: assistantForm.provider_id }); notice = `${models.length} modèles disponibles.`; }
    catch (e) { explain(e); } finally { loadingModels = false; }
  }
  async function saveAssistant() {
    saving = true; error = '';
    try {
      const result = await rpc<Assistant>('assistant.save', { ...assistantForm, id: assistantForm.id || null });
      await refresh(); selectedAssistant = result.id; assistantForm = { ...result }; notice = 'Assistant enregistré. Les conversations existantes conservent leur configuration.';
    } catch (e) { explain(e); } finally { saving = false; }
  }
  async function toggleNotifications() {
    try {
      if (!notifications) notifications = await requestPermission() === 'granted'; else notifications = false;
      localStorage.setItem('nilshell.notifications', String(notifications));
    } catch (e) { explain(e); }
  }
</script>

<div class="app-shell">
  <aside class="sidebar">
    <div class="brand"><span class="brand-icon">n<span>_</span></span><div>Nilshell<small>YOUR LOCAL WORKSPACE</small></div><span class="version">0.1</span></div>
    <button class="new-chat" onclick={newChat} disabled={busy}><span>＋</span> Nouvelle conversation <kbd>↵</kbd></button>
    <div class="section-label">ESPACE DE TRAVAIL</div>
    <nav aria-label="Navigation principale">
      <button class:active={view === 'chat'} onclick={() => view = 'chat'}>◈ <span>Conversations</span><small>{conversations.length}</small></button>
      <button class:active={view === 'assistants'} onclick={() => { view = 'assistants'; editAssistant(); }}>◇ <span>Assistants</span><small>{assistants.length}</small></button>
      <button class:active={view === 'providers'} onclick={() => view = 'providers'}>⌘ <span>Fournisseurs</span><small>{providers.length}</small></button>
      <button class:active={view === 'system'} onclick={() => view = 'system'}>⚙ <span>Système</span></button>
    </nav>
    <div class="section-label history-label">CONVERSATIONS RÉCENTES</div>
    <div class="history">
      {#each conversations as conversation}
        <button class:selected={current?.id === conversation.id} onclick={() => openConversation(conversation.id)} disabled={busy}><span>◦</span>{conversation.title}</button>
      {:else}<p class="muted history-empty">Tes conversations apparaîtront ici.</p>{/each}
    </div>
    <div class="sidebar-footer"><span class="status-dot"></span><span>{desktop ? 'Moteur local' : 'Aperçu de l’interface'}</span><span class="footer-os">DESKTOP</span></div>
  </aside>

  <main>
    <header class="topbar"><div><span class="breadcrumb">Workspace /</span> {view === 'chat' ? 'Conversation' : view === 'providers' ? 'Fournisseurs' : view === 'system' ? 'Système' : 'Assistants'}</div><button class="notification-button" onclick={toggleNotifications} disabled={!desktop} title="Notifications de fin de génération">{notifications ? '◉ Notifications activées' : '◎ Notifications'}</button></header>
    {#if !desktop}<div class="banner">Aperçu visuel — lance <code>npm run desktop dev</code> pour connecter le moteur local.</div>{/if}
    {#if error}<div class="banner error" role="alert">{error}<button onclick={() => error = ''} aria-label="Fermer l’erreur">×</button></div>{/if}
    {#if notice}<div class="banner notice" role="status">{notice}<button onclick={() => notice = ''} aria-label="Fermer le message">×</button></div>{/if}

    {#if view === 'chat'}
      <div class="chat-toolbar"><div class="assistant-picker"><span class="status-dot"></span><select aria-label="Assistant" bind:value={selectedAssistant} disabled={busy || !!current}><option value="" disabled>Choisir un assistant</option>{#each assistants as assistant}<option value={assistant.id}>{assistant.name} · {assistant.model}</option>{/each}</select></div>{#if current}<button class="text-button" onclick={removeConversation} disabled={busy}>Supprimer</button>{:else}<span class="local-badge">HISTORIQUE LOCAL</span>{/if}</div>
      <div class="messages" bind:this={scroller}>
        {#if !current?.messages?.length}
          <section class="welcome"><div class="hero-symbol">n<span>_</span></div><div class="eyebrow">TON BUREAU. TES MODÈLES.</div><h1>Un espace pour penser.<br/><span>Et bientôt, pour agir.</span></h1><p>Discute avec tes modèles et donne à chaque assistant<br class="desktop-break"/> sa personnalité, son rôle et ses instructions.</p>
            <div class="welcome-cards"><button onclick={() => view = 'providers'}><span class="card-icon">⌘</span><strong>Connecter un fournisseur</strong><span>OpenAI, OpenRouter ou Ollama</span><b>↗</b></button><button onclick={() => { view = 'assistants'; editAssistant(); }}><span class="card-icon">◇</span><strong>Créer un assistant</strong><span>Un modèle, un rôle, tes instructions</span><b>↗</b></button></div>
            <div class="privacy-note"><span>▣</span> Conversations sur ton ordinateur · Modèles via ton API</div>
          </section>
        {:else}
          <div class="message-list">{#each current.messages as message}<article class:user={message.role === 'user'} class="message"><div class="avatar">{message.role === 'user' ? 'N' : 'n_'}</div><div class="message-body"><div class="message-author">{message.role === 'user' ? 'Toi' : current.assistant.name}<span>{message.role === 'assistant' ? current.assistant.model : ''}</span></div><div class="markdown">{@html markdown(message.content)}</div>{#if message.status === 'interrupted'}<small class="muted">Réponse interrompue</small>{:else if message.status === 'error'}<small class="muted">Échec de la génération</small>{/if}</div></article>{/each}{#if busy}<div class="generating"><span class="pulse"></span> Génération en cours…</div>{/if}</div>
        {/if}
      </div>
      <div class="composer-area"><form class="composer" onsubmit={event => { event.preventDefault(); send(); }}><textarea aria-label="Message" bind:value={input} placeholder={assistants.length ? 'Écris ton message…' : 'Configure un fournisseur et crée ton premier assistant…'} rows="3" disabled={busy || !assistants.length || !desktop} onkeydown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } }}></textarea><div class="composer-bottom"><span><span class="tiny-diamond">◇</span> {current?.assistant.model ?? assistants.find(a => a.id === selectedAssistant)?.model ?? 'Aucun modèle sélectionné'}</span>{#if busy}<button type="button" class="send-button stop" onclick={cancel}>■ Arrêter</button>{:else}<button class="send-button" disabled={!input.trim() || !selectedAssistant || !desktop} aria-label="Envoyer">↑</button>{/if}</div></form><div class="composer-hint">Entrée pour envoyer <span>·</span> Maj + Entrée pour une nouvelle ligne</div></div>
    {:else if view === 'system'}
      <SystemPanel />
    {:else if view === 'providers'}
      <div class="settings-page"><div class="eyebrow">CONNEXIONS</div><h1>Tes fournisseurs.</h1><p class="subtitle">Choisis où tes modèles s’exécutent. Garde le contrôle de tes connexions.</p><div class="settings-grid"><section class="settings-list"><button class="outline" onclick={() => editProvider()}>＋ Ajouter un fournisseur</button>{#each providers as provider}<button class="entity-card" class:selected={providerForm.id === provider.id} onclick={() => editProvider(provider)}><strong>{provider.name}</strong><span>{provider.base_url}</span><small>{provider.has_key ? 'Clé configurée' : 'Sans clé API'}</small></button>{/each}</section><form class="settings-form" onsubmit={event => { event.preventDefault(); saveProvider(); }}><h2>{providerForm.id ? 'Modifier le fournisseur' : 'Nouveau fournisseur'}</h2><div class="presets"><button type="button" onclick={() => preset('openai')}>OpenAI</button><button type="button" onclick={() => preset('openrouter')}>OpenRouter</button><button type="button" onclick={() => preset('ollama')}>Ollama</button></div><label>Nom<input bind:value={providerForm.name} required maxlength="100" placeholder="Mon fournisseur"/></label><label>Protocole<select bind:value={providerForm.kind}><option value="openai">API compatible OpenAI</option><option value="ollama">API native Ollama</option></select></label><label>URL de base<input bind:value={providerForm.base_url} required type="url" placeholder="https://api.openai.com/v1"/></label><p class="field-help">OpenAI : inclure /v1. Ollama : adresse du serveur sans /api.</p><label>Clé API<input type="password" bind:value={providerForm.api_key} autocomplete="new-password" placeholder={providerForm.id ? 'Laisser vide pour conserver la clé' : 'Optionnelle pour Ollama'} /></label>{#if providerForm.id}<label class="checkbox"><input type="checkbox" bind:checked={providerForm.clear_key}/> Effacer la clé existante</label>{/if}<p class="field-help">Stockée dans le trousseau système si disponible. Jamais dans l’historique.</p><button class="primary" disabled={saving || !desktop}>{saving ? 'Enregistrement…' : 'Enregistrer le fournisseur'}</button></form></div></div>
    {:else}
      <div class="settings-page"><div class="eyebrow">PERSONNALITÉS & INSTRUCTIONS</div><h1>Tes assistants.</h1><p class="subtitle">Un modèle et un prompt système pour chaque façon de travailler.</p>{#if !providers.length}<div class="empty-panel"><p>Connecte un fournisseur pour créer ton premier assistant.</p><button class="primary" onclick={() => view = 'providers'}>Configurer un fournisseur →</button></div>{:else}<div class="settings-grid"><section class="settings-list"><button class="outline" onclick={() => editAssistant()}>＋ Nouvel assistant</button>{#each assistants as assistant}<button class="entity-card" class:selected={assistantForm.id === assistant.id} onclick={() => editAssistant(assistant)}><strong>{assistant.name}</strong><span>{assistant.model}</span></button>{/each}</section><form class="settings-form" onsubmit={event => { event.preventDefault(); saveAssistant(); }}><h2>{assistantForm.id ? 'Modifier l’assistant' : 'Nouvel assistant'}</h2><label>Nom<input bind:value={assistantForm.name} required maxlength="100" placeholder="Ada, Gaspard…"/></label><label>Fournisseur<select bind:value={assistantForm.provider_id} onchange={() => { models = []; assistantForm.model = ''; }} required><option value="" disabled>Choisir un fournisseur</option>{#each providers as provider}<option value={provider.id}>{provider.name}</option>{/each}</select></label><label>Modèle<div class="input-action"><input bind:value={assistantForm.model} list="models" required maxlength="300" placeholder="Identifiant du modèle"/><button type="button" class="outline" onclick={loadModels} disabled={loadingModels || !assistantForm.provider_id}>{loadingModels ? '…' : 'Charger'}</button></div><datalist id="models">{#each models as model}<option value={model}></option>{/each}</datalist></label><p class="field-help">Charge la liste ou saisis directement l’identifiant du modèle.</p><label>Prompt système<textarea bind:value={assistantForm.system_prompt} rows="9" placeholder="Tu es un assistant…" maxlength="100000"></textarea></label><button class="primary" disabled={saving || !desktop}>{saving ? 'Enregistrement…' : 'Enregistrer l’assistant'}</button></form></div>{/if}</div>
    {/if}
  </main>
</div>
