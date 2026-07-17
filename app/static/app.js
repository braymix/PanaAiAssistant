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
     'approval_requested', 'approval_resolved', 'plan_done', 'error', 'chat_delta']
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
    const addBubble = (role, text) => {
      const wrap = document.createElement('div'); wrap.className = 'msg ' + role;
      const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text;
      wrap.appendChild(b); messages.appendChild(wrap);
      wrap.scrollIntoView(); return b;
    };
    let current = null;
    subscribeGlobal((ev) => {
      if (ev.kind === 'chat_delta') {
        const d = JSON.parse(ev.data);
        if (d.conversation_id !== cid) return;
        if (!current) current = addBubble('assistant', '');
        current.textContent += d.text;
      }
      if (ev.kind === 'chat_done') current = null;
    });
    document.getElementById('send').onclick = async () => {
      const text = input.value.trim(); if (!text) return;
      addBubble('user', text); input.value = ''; current = null;
      await jpost('/chat/send', { conversation_id: cid, text, repo_path: repo });
    };
    document.getElementById('via').onclick = async () => {
      try {
        const r = await jpost('/plans/via', { conversation_id: cid, repo_path: repo || '.' });
        location.href = r.next;
      } catch (e) { alert('Piano rifiutato: ' + e.message); }
    };
  }

  async function approvePlan(planId) {
    await jpost('/plans/' + planId + '/approve', {});
  }

  // monitor live del piano: aggiorna i badge dei task e le approvazioni pendenti
  function monitorPlan(planId) {
    const STYLE = {
      pending: 'b-pending', running: 'b-running', verifying: 'b-running',
      done: 'b-done', failed: 'b-failed', escalated: 'b-esc',
    };
    async function tick() {
      let s;
      try { s = await (await fetch('/plans/' + planId + '/status')).json(); }
      catch (e) { return; }        // rete giu' (§4.15): riprova
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
      if (['done', 'failed'].includes(s.plan_status)) clearInterval(timer);
    }
    tick();
    const timer = setInterval(tick, 2500);
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
    const repo = document.getElementById('p-repo').value.trim();
    if (!name || !repo) { alert('nome e repo_path richiesti'); return; }
    try {
      await jpost('/projects', { name, repo_path: repo });
      location.reload();
    } catch (e) { alert(e.message); }
  }

  async function newChatWithProject(mode) {
    const sel = document.getElementById('project');
    const repo = sel ? sel.value : '';
    const r = await jpost('/chat/new', { repo_path: repo, mode: mode || 'generic' });
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
    async function tick() {
      try {
        const s = await (await fetch('/ollama/ps')).json();
        if (s.error) { el.textContent = s.error; return; }
        const models = s.models || [];
        if (!models.length) { el.textContent = 'nessun modello caricato ora.'; return; }
        el.innerHTML = '';
        models.forEach((m) => {
          const gpu = (m.size_vram && m.size)
            ? Math.round(100 * m.size_vram / m.size) : null;
          const d = document.createElement('div'); d.className = 'ev';
          d.textContent = m.name + (gpu !== null ? '  ·  ' + gpu + '% GPU' : '');
          el.appendChild(d);
        });
      } catch (e) { el.textContent = 'Ollama non raggiungibile.'; }
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

  // registra il SW ovunque (serve per PWA/push)
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  return { pollStats, subscribeGlobal, appendLog, wireChat, approvePlan,
           monitorPlan, streamRun, newChat, decide, wireInstall, enablePush,
           loadProjects, addProject, newChatWithProject,
           streamOllamaLogs, pollOllamaPs, deleteChat };
})();
