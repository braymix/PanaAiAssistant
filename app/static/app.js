/* Argo client — niente build step, niente npm. Vanilla. */
const Argo = (() => {
  async function jpost(url, body) {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  }

  // --- stats (M5) ---
  async function pollStats() {
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    async function tick() {
      try {
        const s = await (await fetch('/stats')).json();
        set('s-active', s.active_runs); set('s-cost', '€' + s.cost_today_eur);
        set('s-queue', s.ollama_queue); set('s-pending', s.pending_approvals);
        set('s-done', s.tasks_done); set('s-failed', s.tasks_failed);
        set('s-esc', s.tasks_escalated); set('s-push', s.pushes_sent);
      } catch (e) { /* rete giu' (§4.15): riprova al prossimo tick */ }
    }
    tick(); setInterval(tick, 4000);
  }

  // --- SSE globale con Last-Event-ID (§4.15) ---
  function subscribeGlobal(onEvent) {
    const es = new EventSource('/events');
    ['assistant_text', 'tool_use', 'verify_result', 'escalation', 'result',
     'approval_requested', 'approval_resolved', 'plan_done', 'error',
     'chat_delta', 'chat_done',
     'route_decision', 'config_warning', 'task_cancelled', 'plan_cancelled',
     'queue_paused', 'queue_resumed', 'task_blocked', 'task_unblocked',
     'conversation_deleted', 'plan_deleted', 'task_deleted', 'run_deleted',
     'conversation_purged', 'plan_purged', 'conversation_renamed',
     'system_app_restart', 'system_app_shutdown', 'system_pc_shutdown',
     'system_services_restarted', 'system_reset']
      .forEach((k) => es.addEventListener(k, (e) => onEvent({ kind: k, data: e.data })));
    return es;
  }

  function appendLog(id, ev) {
    const el = document.getElementById(id);
    if (!el) return;
    const line = document.createElement('div');
    line.textContent = ev.kind + ': ' + ev.data;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }

  // --- chat + VIA (M3) ---
  function wireChat(cid, repo) {
    const input = document.getElementById('input');
    const messages = document.getElementById('messages');
    const sendBtn = document.getElementById('send');
    const viaBtn = document.getElementById('via');
    const addBubble = (role, text) => {
      const wrap = document.createElement('div'); wrap.className = 'msg ' + role;
      const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text;
      wrap.appendChild(b); messages.appendChild(wrap);
      wrap.scrollIntoView(); return b;
    };
    let current = null;      // bolla assistant in costruzione
    let waiting = false;     // turno in corso: attende delta dal planner
    // indicatore "sta scrivendo…": appare finche' non arriva il primo delta
    const setBusy = (on) => {
      waiting = on;
      sendBtn.disabled = on;
      sendBtn.textContent = on ? 'Invio…' : 'Invia';
      let ind = document.getElementById('typing');
      if (on && !ind) {
        ind = document.createElement('div');
        ind.id = 'typing'; ind.className = 'msg assistant typing';
        ind.innerHTML = '<div class="bubble">il planner sta scrivendo…</div>';
        messages.appendChild(ind); ind.scrollIntoView();
      } else if (!on && ind) {
        ind.remove();
      }
    };
    subscribeGlobal((ev) => {
      if (ev.kind === 'chat_delta') {
        const d = JSON.parse(ev.data);
        if (d.conversation_id !== cid) return;
        const ind = document.getElementById('typing');
        if (ind) ind.remove();               // primo pezzo: via l'indicatore
        if (!current) current = addBubble('assistant', '');
        current.textContent += d.text;
        current.scrollIntoView();
      }
      if (ev.kind === 'chat_done') {
        let d = {}; try { d = JSON.parse(ev.data); } catch (e) {}
        if (d.conversation_id && d.conversation_id !== cid) return;
        current = null; setBusy(false);       // turno finito: sblocca l'invio
      }
      if (ev.kind === 'error' && waiting) {
        // il turno e' fallito lato server: sblocca e mostra il motivo
        let d = {}; try { d = JSON.parse(ev.data); } catch (e) {}
        setBusy(false); current = null;
        addBubble('assistant', '⚠ ' + (d.detail || 'errore del planner'));
      }
    });
    const send = async () => {
      if (waiting) return;                     // un turno alla volta
      const text = input.value.trim(); if (!text) return;
      addBubble('user', text); input.value = ''; current = null;
      setBusy(true);
      try {
        await jpost('/chat/send', { conversation_id: cid, text, repo_path: repo });
      } catch (e) {
        setBusy(false);
        addBubble('assistant', '⚠ invio fallito: ' + e.message);
      }
    };
    sendBtn.onclick = send;
    // Invio = manda, Shift+Invio = a capo (comodo da tastiera fisica)
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    viaBtn.onclick = async () => {
      viaBtn.disabled = true; const label = viaBtn.textContent;
      viaBtn.textContent = 'Genero il piano…';
      try {
        const r = await jpost('/plans/via', { conversation_id: cid, repo_path: repo || '.' });
        location.href = r.next;
      } catch (e) {
        viaBtn.disabled = false; viaBtn.textContent = label;
        alert('Piano rifiutato: ' + e.message);
      }
    };
  }

  async function approvePlan(planId) {
    await jpost('/plans/' + planId + '/approve', {});
  }

  // etichetta umana dello stato del piano: chiarisce QUANDO diventa effettivo.
  // 'draft' = proposta non ancora avviata; 'executing' = in corso; ecc.
  const PLAN_LABEL = {
    draft: 'Bozza — non ancora avviato',
    executing: 'In esecuzione',
    running: 'In esecuzione',
    done: 'Completato',
    failed: 'Fallito',
    cancelled: 'Annullato',
  };
  // aggiorna il banner di stato del piano (#plan-state), se presente in pagina
  function renderPlanState(status) {
    const el = document.getElementById('plan-state');
    if (!el) return;
    el.textContent = PLAN_LABEL[status] || status;
    el.className = 'plan-state badge b-' + status;
    const btn = document.getElementById('approve');
    // a piano avviato/finito il tasto Esegui non ha piu' senso: nascondilo
    if (btn && status !== 'draft') btn.hidden = true;
  }

  // monitor live del piano: aggiorna i badge dei task e le approvazioni pendenti
  function monitorPlan(planId) {
    const STYLE = {
      pending: 'b-pending', running: 'b-running', verifying: 'b-running',
      done: 'b-done', failed: 'b-failed', escalated: 'b-esc',
      cancelled: 'b-failed', blocked: 'b-pending',
    };
    async function tick() {
      let s;
      try { s = await (await fetch('/plans/' + planId + '/status')).json(); }
      catch (e) { return; }        // rete giu' (§4.15): riprova
      renderPlanState(s.plan_status);
      (s.tasks || []).forEach((t) => {
        const li = document.querySelector('[data-task="' + t.id + '"]');
        if (!li) return;
        const badge = li.querySelector('[data-status]');
        badge.textContent = t.status + (t.attempts ? ' ·' + t.attempts : '');
        badge.className = 'badge ' + (STYLE[t.status] || '');
        // collega il titolo alla pagina del run (log live) quando esiste
        const titleEl = li.querySelector('.t-title');
        if (t.run_id && titleEl && !titleEl.querySelector('a')) {
          titleEl.innerHTML = '<a href="/runs/' + t.run_id + '">' + titleEl.textContent + '</a>';
        }
      });
      const box = document.getElementById('pending');
      const list = document.getElementById('pending-list');
      if (box && list) {
        list.innerHTML = '';
        (s.pending_approvals || []).forEach((a) => {
          const li = document.createElement('li');
          li.innerHTML = '<a href="/approvals/' + a.id + '">▶ ' + a.tool_name +
            ' — ' + (a.task_title || '') + '</a>';
          list.appendChild(li);
        });
        box.hidden = (s.pending_approvals || []).length === 0;
      }
      if (['done', 'failed', 'cancelled'].includes(s.plan_status)) clearInterval(timer);
    }
    tick();
    const timer = setInterval(tick, 2500);
  }

  // --- ciclo di vita (§B): annulla / blocca / elimina / purga --------------
  async function jsend(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json().catch(() => ({}));
  }
  const cancelPlan = (id) => jpost('/plans/' + id + '/cancel', {});
  const cancelTask = (id) => jpost('/tasks/' + id + '/cancel', {});
  const blockTask = (id) => jpost('/tasks/' + id + '/block', {});
  const unblockTask = (id) => jpost('/tasks/' + id + '/unblock', {});
  const softDeletePlan = (id) => jsend('DELETE', '/plans/' + id);
  const softDeleteConversation = (id) => jsend('DELETE', '/conversations/' + id);
  const purgePlan = (id) => jpost('/plans/' + id + '/purge', { confirm: true });
  const purgeConversation = (id) =>
    jpost('/conversations/' + id + '/purge', { confirm: true });
  const pauseQueue = () => jpost('/queue/pause', {});
  const resumeQueue = () => jpost('/queue/resume', {});
  const stopRun = (id) => jpost('/runs/' + id + '/stop', {});
  const deleteRun = (id) => jsend('DELETE', '/runs/' + id);

  // STOP + elimina un run dalla lista/dettaglio. Se backHref e' dato, naviga li'
  // dopo l'eliminazione; altrimenti rimuove la riga [data-run] dal DOM.
  async function runAction(action, runId, backHref) {
    try {
      if (action === 'stop') {
        if (!confirm('Fermare questo run?')) return;
        await stopRun(runId);
        return;
      }
      if (action === 'delete') {
        if (!confirm('Eliminare questo run? (rimuove log ed eventi, irreversibile)')) return;
        await deleteRun(runId);
        if (backHref) { location.href = backHref; return; }
        const li = document.querySelector('[data-run="' + runId + '"]');
        if (li) li.remove();
      }
    } catch (e) { alert(e.message); }
  }

  // elimina un piano dalla lista (soft-delete) senza aprirlo.
  async function planListAction(action, planId) {
    try {
      if (action === 'cancel') { await cancelPlan(planId); return; }
      if (action === 'delete') {
        if (!confirm('Eliminare il piano? (reversibile, resta nel log)')) return;
        await softDeletePlan(planId);
        const li = document.querySelector('[data-plan="' + planId + '"]');
        if (li) li.remove();
      }
    } catch (e) { alert(e.message); }
  }

  // toggle globale di pausa coda: aggiorna il bottone dallo stato reale (/queue)
  async function wireQueuePause(btnId) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    async function render() {
      try {
        const s = await (await fetch('/queue', { cache: 'no-store' })).json();
        btn.dataset.paused = s.paused ? '1' : '0';
        btn.textContent = s.paused ? '▶ Riprendi coda' : '⏸ Pausa coda';
        btn.classList.toggle('is-paused', !!s.paused);
      } catch (e) { /* rete giu' (§4.15) */ }
    }
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        if (btn.dataset.paused === '1') await resumeQueue(); else await pauseQueue();
      } catch (e) { alert(e.message); }
      btn.disabled = false; render();
    };
    render();
  }

  // menu azioni su un task nella pagina piano (annulla/blocca/elimina)
  async function taskAction(action, taskId) {
    try {
      if (action === 'cancel') await cancelTask(taskId);
      else if (action === 'block') await blockTask(taskId);
      else if (action === 'unblock') await unblockTask(taskId);
    } catch (e) { alert(e.message); }
  }

  async function planDanger(action, planId, backHref) {
    try {
      if (action === 'cancel') { await cancelPlan(planId); return; }
      if (action === 'delete') {
        if (!confirm('Eliminare il piano? (reversibile, resta nel log)')) return;
        await softDeletePlan(planId);
      } else if (action === 'purge') {
        if (!confirm('ELIMINA DEFINITIVAMENTE: rimuove piano, task, run ed eventi. '
                     + 'Irreversibile. Confermi?')) return;
        if (!confirm('Ultima conferma: procedo con il purge definitivo?')) return;
        await purgePlan(planId);
      }
      location.href = backHref || '/plans';
    } catch (e) { alert(e.message); }
  }

  async function newChat() {
    const r = await jpost('/chat/new', {});
    location.href = '/chat/' + r.conversation_id;
  }

  // --- progetti ---
  async function loadProjects() {
    const sel = document.getElementById('project');
    if (!sel) return;
    try {
      const projs = await (await fetch('/projects')).json();
      projs.forEach((p) => {
        const o = document.createElement('option');
        o.value = p.repo_path; o.textContent = p.name + ' — ' + p.repo_path;
        sel.appendChild(o);
      });
    } catch (e) { /* rete giu' */ }
  }

  async function addProject() {
    const name = document.getElementById('p-name').value.trim();
    // repo_path e' opzionale (§A.3): senza, il progetto vive in document_root/name.
    const repo = document.getElementById('p-repo').value.trim();
    if (!name) { alert('il nome e\' richiesto'); return; }
    try {
      await jpost('/projects', { name, repo_path: repo || null });
      location.reload();
    } catch (e) { alert(e.message); }
  }

  async function newChatWithProject(mode) {
    const sel = document.getElementById('project');
    // repo attivo: dal vecchio select se presente, altrimenti dalla pagina Progetti
    const repo = (sel ? sel.value : '') || localStorage.getItem('argo_repo') || '';
    const r = await jpost('/chat/new', { repo_path: repo, mode: mode || 'claudio_codice' });
    const q = repo ? ('?repo=' + encodeURIComponent(repo)) : '';
    location.href = '/chat/' + r.conversation_id + q;
  }

  // --- dettaglio run: log live via SSE per-run (/runs/{id}/events) ---
  function streamRun(runId) {
    const log = document.getElementById('runlog');
    if (!log) return;
    const add = (cls, text) => {
      const d = document.createElement('div');
      d.className = 'ev ' + cls; d.textContent = text;
      log.appendChild(d); log.scrollTop = log.scrollHeight;
    };
    const es = new EventSource('/runs/' + runId + '/events');
    const parse = (e) => { try { return JSON.parse(e.data); } catch (x) { return {}; } };
    es.addEventListener('assistant_text', (e) => add('txt', parse(e).text || ''));
    es.addEventListener('tool_use', (e) => {
      const d = parse(e); add('tool', '⚙ ' + d.name + ' ' + JSON.stringify(d.input || {}));
    });
    es.addEventListener('policy_auto_allow', (e) => add('auto', '✓ auto: ' + (parse(e).tool_name || '')));
    es.addEventListener('policy_deny', (e) => add('deny', '✗ deny: ' + (parse(e).reason || '')));
    es.addEventListener('approval_requested', (e) => add('appr', '⏸ approvazione: ' + (parse(e).tool_name || '')));
    es.addEventListener('approval_resolved', (e) => add('appr', '▶ ' + (parse(e).status || '')));
    es.addEventListener('verify_result', (e) => {
      const d = parse(e); add(d.passed ? 'ok' : 'fail',
        (d.passed ? '✅ verify ok' : '❌ verify fallito') + '\n' + (d.output_tail || ''));
    });
    es.addEventListener('result', (e) => {
      const d = parse(e); add('res', 'result ' + d.subtype + ' · turni ' + d.turns + ' · $' + d.cost_usd);
    });
    es.addEventListener('error', (e) => add('fail', 'errore: ' + (parse(e).detail || '')));
  }

  // --- Ollama: log live + modelli caricati ---
  function streamOllamaLogs() {
    const box = document.getElementById('ollog');
    if (!box) return;
    const es = new EventSource('/ollama/logs/stream');
    es.addEventListener('logline', (e) => {
      const d = document.createElement('div');
      d.className = 'ev'; d.textContent = e.data;
      box.appendChild(d);
      while (box.childNodes.length > 500) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
    });
  }

  async function pollOllamaPs() {
    const el = document.getElementById('ps');
    if (!el) return;
    const empty = (msg) => { el.classList.add('muted'); el.textContent = msg; };
    async function tick() {
      try {
        const s = await (await fetch('/ollama/ps')).json();
        if (s.error) { empty(s.error); return; }
        const models = s.models || [];
        if (!models.length) { empty('nessun modello caricato ora.'); return; }
        el.classList.remove('muted');
        el.innerHTML = '';
        models.forEach((m) => {
          const gpu = (m.size_vram && m.size)
            ? Math.round(100 * m.size_vram / m.size) : null;
          const d = document.createElement('div'); d.className = 'ev';
          const name = document.createElement('span'); name.textContent = m.name;
          d.appendChild(name);
          if (gpu !== null) {
            const g = document.createElement('span');
            g.className = 'gpu'; g.textContent = gpu + '% GPU';
            d.appendChild(g);
          }
          el.appendChild(d);
        });
      } catch (e) { empty('Ollama non raggiungibile.'); }
    }
    tick(); setInterval(tick, 3000);
  }

  async function deleteChat(id, ev) {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    if (!confirm('Eliminare questa chat?')) return;
    try {
      const r = await fetch('/chat/' + id, { method: 'DELETE' });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const li = document.querySelector('[data-conv="' + id + '"]');
      if (li) li.remove();
    } catch (e) { alert(e.message); }
  }

  // rinomina il titolo di una chat (dalla lista in home). Legge il titolo
  // corrente dal DOM per evitare problemi di escaping negli attributi.
  async function renameChat(id, ev) {
    if (ev) { ev.preventDefault(); ev.stopPropagation(); }
    const el = document.querySelector('[data-conv="' + id + '"] .rt');
    const current = el ? el.textContent.trim() : '';
    const title = prompt('Nuovo titolo della chat:', current);
    if (title === null) return;
    const t = title.trim();
    if (!t) return;
    try {
      const r = await fetch('/chat/' + id, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: t }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      if (el) el.textContent = t;
    } catch (e) { alert(e.message); }
  }

  // --- comandi di sistema (§ sistema): azioni distruttive, doppia conferma ---
  async function systemAction(action) {
    const MSG = {
      'app/restart': 'Riavviare l\'app? Il servizio si ferma e riparte da solo.',
      'app/shutdown': 'Spegnere l\'app? Il servizio si ferma (poi va riavviato dal PC).',
      'pc/shutdown': 'SPEGNERE IL PC? Tutto si spegne.',
      'services/restart': 'Riavviare i servizi interni? Annulla il lavoro in corso.',
    };
    if (!confirm(MSG[action] || 'Confermi?')) return;
    if (action === 'pc/shutdown' && !confirm('Ultima conferma: spengo il PC?')) return;
    try {
      const r = await jpost('/system/' + action, { confirm: true });
      alert('OK: ' + (r.status || 'fatto'));
    } catch (e) { alert('Errore: ' + e.message); }
  }

  async function systemReset() {
    if (!confirm('PULIZIA TOTALE del DB + riavvio dei servizi.\n'
                 + 'Elimina chat, piani, task, run ed eventi. Irreversibile.')) return;
    if (!confirm('Ultima conferma: procedo con la pulizia totale?')) return;
    try {
      const r = await jpost('/system/reset', { confirm: true });
      const rm = r.removed || {};
      const n = Object.values(rm).reduce((a, b) => a + b, 0);
      alert('Reset completato: ' + n + ' righe rimosse. Servizi riavviati.');
      location.href = '/';
    } catch (e) { alert('Errore: ' + e.message); }
  }

  // --- approvazione (M2) ---
  async function decide(id, allow) {
    try {
      await jpost('/approvals/' + id + '/decide', { allow });
      const s = document.getElementById('apr-status');
      if (s) s.textContent = 'stato: ' + (allow ? 'allowed' : 'denied');
      const bar = document.querySelector('.approve-bar'); if (bar) bar.remove();
    } catch (e) { alert(e.message); }
  }

  // --- PWA install + push (GATE 0 / M2) ---
  let deferredPrompt = null;
  function wireInstall(id) {
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault(); deferredPrompt = e;
    });
    const btn = document.getElementById(id);
    if (btn) btn.onclick = async () => {
      if (deferredPrompt) { deferredPrompt.prompt(); deferredPrompt = null; }
      else alert('iOS: Condividi → Aggiungi a Home.');
    };
  }

  function b64ToU8(b64) {
    const pad = '='.repeat((4 - (b64.length % 4)) % 4);
    const s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from([...atob(s)].map((c) => c.charCodeAt(0)));
  }

  async function enablePush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      alert('Push non supportata qui.'); return;
    }
    const reg = await navigator.serviceWorker.register('/sw.js');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') return;
    const { publicKey } = await (await fetch('/push/vapid-public-key')).json();
    if (!publicKey) { alert('Chiavi VAPID assenti sul server (gen_vapid.py).'); return; }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: b64ToU8(publicKey),
    });
    await jpost('/push/subscribe', sub.toJSON());
    alert('Push attivata.');
  }

  // --- PC Agenti (§C.5): stato live delle card in dashboard ---
  async function pollAgents() {
    async function tick() {
      let list = [];
      try { list = (await (await fetch('/agents')).json()).agents || []; }
      catch (e) { return; }
      list.forEach((a) => {
        const el = document.querySelector('[data-agent-state="' + a.name + '"]');
        if (!el) return;
        const dot = el.querySelector('.dot'); const lbl = el.querySelector('.lbl');
        dot.classList.toggle('off', !a.running);
        dot.classList.toggle('on', !!a.running);
        if (lbl) lbl.textContent = a.running ? 'Attivo' : 'Fermo';
      });
    }
    tick(); setInterval(tick, 5000);
  }

  // --- pagina agente (OpenClaw): azioni + task + log live via SSE ---
  function wireAgent(name) {
    const $ = (id) => document.getElementById(id);
    const setBusy = (b) => { document.querySelectorAll('.agent-actions .btn')
      .forEach((x) => { x.disabled = b; }); };

    async function refreshStatus() {
      try {
        const s = await (await fetch('/agents/' + name + '/status')).json();
        const dot = $('ag-dot'), lbl = $('ag-state');
        if (dot) { dot.classList.toggle('on', !!s.process_running);
                   dot.classList.toggle('off', !s.process_running); }
        if (lbl) lbl.textContent = s.installed
          ? (s.process_running ? '🟢 Attivo' : '🔴 Fermo')
          : '⚠️ Non installato';
        const set = (id, v) => { const e = $(id); if (e) e.textContent = (v ?? '—'); };
        set('ag-version', s.version || '—');
        set('ag-primary', s.primary_model || '—');
        set('ag-ollama', s.ollama_connected ? 'connesso' : 'offline');
      } catch (e) { /* rete giu': riprova al prossimo giro */ }
    }

    async function act(path, okMsg) {
      setBusy(true);
      try { await jpost('/agents/' + name + '/' + path, {}); if (okMsg) toast(okMsg); }
      catch (e) { alert(e.message); }
      finally { setBusy(false); refreshStatus(); }
    }
    async function ocPost(path, body, okMsg) {
      setBusy(true);
      try { const r = await jpost(path, body || {}); if (okMsg) toast(okMsg); return r; }
      catch (e) { alert(e.message); }
      finally { setBusy(false); refreshStatus(); }
    }
    function toast(m) { const t = $('ag-toast'); if (t) { t.textContent = m;
      t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2500); } }

    if ($('ag-start')) $('ag-start').onclick = () => act('start', 'Avviato');
    if ($('ag-stop')) $('ag-stop').onclick = () => act('stop', 'Fermato');
    if ($('ag-restart')) $('ag-restart').onclick = () => act('restart', 'Riavviato');
    if ($('ag-setup')) $('ag-setup').onclick = () => ocPost('/openclaw/setup', {}, 'Scaricato & configurato (vedi log)');
    if ($('ag-sync')) $('ag-sync').onclick = async () => {
      const r = await ocPost('/openclaw/config/sync', {}, null);
      if (r) toast(r.n_models + ' modelli sincronizzati');
    };
    if ($('ag-send')) $('ag-send').onclick = async () => {
      const ta = $('ag-task'); const prompt = (ta.value || '').trim();
      if (!prompt) { alert('Scrivi un task.'); return; }
      try { const r = await jpost('/agents/' + name + '/task', { prompt });
        toast('Task inviato: ' + r.task_id); ta.value = ''; }
      catch (e) { alert(e.message); }
    };

    // log live via SSE + pause/resume + auto-scroll
    const logEl = $('ag-log');
    let paused = false, es = null;
    function connect() {
      es = new EventSource('/agents/' + name + '/logs/stream');
      es.addEventListener('logline', (e) => {
        if (paused || !logEl) return;
        const d = document.createElement('div'); d.className = 'logline';
        d.textContent = e.data; logEl.appendChild(d);
        while (logEl.childNodes.length > 800) logEl.removeChild(logEl.firstChild);
        logEl.scrollTop = logEl.scrollHeight;
      });
    }
    if ($('ag-log-pause')) $('ag-log-pause').onclick = (ev) => {
      paused = !paused; ev.target.textContent = paused ? '▶ Riprendi' : '⏸ Pausa'; };
    if (logEl) connect();

    refreshStatus(); setInterval(refreshStatus, 5000);
  }

  // registra il SW ovunque (serve per PWA/push)
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  return { pollStats, pollAgents, wireAgent, subscribeGlobal, appendLog, wireChat, approvePlan,
           monitorPlan, renderPlanState, streamRun, newChat, decide, wireInstall, enablePush,
           loadProjects, addProject, newChatWithProject,
           streamOllamaLogs, pollOllamaPs, deleteChat, renameChat,
           systemAction, systemReset,
           cancelPlan, cancelTask, blockTask, unblockTask,
           softDeletePlan, softDeleteConversation, purgePlan, purgeConversation,
           pauseQueue, resumeQueue, wireQueuePause, taskAction, planDanger,
           stopRun, deleteRun, runAction, planListAction };
})();
