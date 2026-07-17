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
        set('s-active', s.active_runs); set('s-cost', '$' + s.cost_today);
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
    location.href = '/';
  }

  async function newChat() {
    const r = await jpost('/chat/new', {});
    location.href = '/chat/' + r.conversation_id;
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
           newChat, decide, wireInstall, enablePush };
})();
