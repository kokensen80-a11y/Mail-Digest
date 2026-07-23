(() => {
  const root = document.documentElement;
  const body = document.body;
  const storageKey = 'able-visual-prototype-v1';
  const defaultState = {
    theme: 'forest',
    ambient: true,
    onboarded: false,
    focusProtected: false,
    tasks: [{ id: 1, title: 'Stuur projectnotities naar Maya', complete: false }]
  };
  let state = { ...defaultState, tasks: defaultState.tasks.map((task) => ({ ...task })) };
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved && typeof saved === 'object') state = { ...state, ...saved, tasks: Array.isArray(saved.tasks) ? saved.tasks : state.tasks };
  } catch {
    state = { ...defaultState, tasks: defaultState.tasks.map((task) => ({ ...task })) };
  }

  const save = () => {
    try { localStorage.setItem(storageKey, JSON.stringify(state)); } catch { /* Prototype remains usable without storage. */ }
  };
  const qs = (selector, parent = document) => parent.querySelector(selector);
  const qsa = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
  const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  })[character]);

  const loginView = qs('[data-login-view]');
  const appView = qs('[data-app-view]');
  const appMain = qs('#app-main');
  const sheetBackdrop = qs('[data-sheet-backdrop]');
  const sheetContent = qs('[data-sheet-content]');
  const chatOverlay = qs('[data-chat-overlay]');
  const toast = qs('[data-toast]');
  const toastText = qs('[data-toast-text]');
  const toastAction = qs('[data-toast-action]');
  const loadingOverlay = qs('[data-loading-overlay]');
  const isDemoMode = window.location.protocol === 'file:'
    || window.location.hostname === 'localhost'
    || window.location.hostname === '127.0.0.1';
  let activeScreen = 'home';
  let toastTimer;
  let undoAction = null;
  let lastFocusedElement = null;
  let voiceTimers = [];
  let isRefreshing = false;
  let pullStartY = null;
  let pullDistance = 0;

  // --- Echte backend (Able) ---------------------------------------------------
  const api = (path, opts = {}) => fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
  }, opts));
  let currentUser = null;
  const MONTHS_NL = ['januari', 'februari', 'maart', 'april', 'mei', 'juni', 'juli',
    'augustus', 'september', 'oktober', 'november', 'december'];
  const DAYS_NL = ['zondag', 'maandag', 'dinsdag', 'woensdag', 'donderdag', 'vrijdag', 'zaterdag'];
  const cap = (value) => value.charAt(0).toUpperCase() + value.slice(1);

  const applyUser = (user) => {
    currentUser = user || null;
    const name = (user && user.name) ? user.name : '';
    const now = new Date();
    const hour = now.getHours();
    const greet = hour < 6 ? 'Goedenacht' : hour < 12 ? 'Goedemorgen'
      : hour < 18 ? 'Goedemiddag' : 'Goedenavond';
    const title = qs('#home-title');
    if (title) title.textContent = name ? `${greet}, ${name}` : greet;
    const kicker = qs('#screen-home .kicker');
    if (kicker) kicker.textContent = `${cap(DAYS_NL[now.getDay()])}, ${now.getDate()} ${MONTHS_NL[now.getMonth()]}`;
    applyAvatar();
  };
  const applyAvatar = () => {
    const initial = (currentUser && currentUser.name ? currentUser.name : '?').slice(0, 1).toUpperCase();
    const src = currentUser && currentUser.avatar;
    qsa('[data-av-img]').forEach((img) => {
      if (src) { img.src = src; img.hidden = false; } else { img.removeAttribute('src'); img.hidden = true; }
    });
    qsa('[data-av-initial]').forEach((el) => { el.textContent = initial; el.hidden = !!src; });
  };

  const loginForm = qs('[data-login-form]');
  let loginErrorEl = null;
  const showLoginError = (message) => {
    if (!loginForm) return;
    if (!loginErrorEl) {
      loginErrorEl = document.createElement('p');
      loginErrorEl.className = 'login-error';
      loginErrorEl.setAttribute('role', 'alert');
      loginForm.appendChild(loginErrorEl);
    }
    loginErrorEl.textContent = message;
  };
  const clearLoginError = () => { if (loginErrorEl) loginErrorEl.textContent = ''; };

  // --- Home-data (echte agenda / taken / follow-ups) --------------------------
  const relTime = (iso) => {
    if (!iso) return '';
    const diffMin = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
    if (diffMin < 0) return '';
    if (diffMin < 60) return `over ${diffMin} min`;
    const hrs = Math.round(diffMin / 60);
    if (hrs < 24) return `over ${hrs} uur`;
    const days = Math.round(hrs / 24);
    return days <= 1 ? 'morgen' : `over ${days} dagen`;
  };
  const isSameDay = (iso) => {
    if (!iso) return false;
    const d = new Date(iso); const n = new Date();
    return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  };
  const setText = (selector, value) => { const el = qs(selector); if (el) el.textContent = value; };

  const loadHome = async () => {
    let agenda = [];
    try { agenda = (await (await api('/api/agenda')).json()).items || []; } catch (e) {}
    const nowMs = Date.now();
    const upcoming = agenda.filter((x) => !x.iso || new Date(x.iso).getTime() >= nowMs - 3600000);
    const next = upcoming[0];
    if (next) {
      setText('[data-hero-time]', next.time || '—');
      setText('[data-hero-title]', next.title || 'Afspraak');
      setText('[data-hero-sub]', next.day || '');
      setText('[data-hero-rel]', isSameDay(next.iso) ? (relTime(next.iso) || 'vandaag') : (next.day || ''));
    } else {
      setText('[data-hero-time]', '—');
      setText('[data-hero-title]', 'Geen afspraken');
      setText('[data-hero-sub]', 'Je agenda is rustig');
      setText('[data-hero-rel]', '');
    }
    setText('[data-count-appointments]', String(agenda.filter((x) => isSameDay(x.iso)).length));

    try {
      const mt = await (await api('/api/mailtab')).json();
      setText('[data-count-waiting]', String((mt.followups || []).length));
    } catch (e) {}

    await loadTasks();

    try {
      const hist = (await (await api('/api/history')).json()).items || [];
      const lastUser = [...hist].reverse().find((m) => m.role === 'user');
      if (lastUser && lastUser.content) {
        setText('[data-recent-title]', lastUser.content.slice(0, 46));
        setText('[data-recent-sub]', `${hist.length} berichten`);
      } else {
        setText('[data-recent-title]', 'Begin een gesprek');
        setText('[data-recent-sub]', 'Able kent je dag al');
      }
    } catch (e) {}
  };

  const applyTheme = (theme) => {
    state.theme = theme === 'light' ? 'light' : 'forest';
    root.dataset.theme = state.theme;
    const isForest = state.theme === 'forest';
    qsa('meta[name="theme-color"]').forEach((meta) => meta.setAttribute('content', isForest ? '#09332C' : '#F3F3F2'));
    qsa('[data-theme-label]').forEach((label) => { label.textContent = isForest ? 'Able Forest' : 'Able Light'; });
    const loginThemeButton = qs('[data-theme-toggle]');
    if (loginThemeButton) loginThemeButton.textContent = isForest ? 'Bekijk Able Light' : 'Bekijk Able Forest';
    save();
  };

  const applyAmbient = () => {
    body.classList.toggle('ambient-on', state.ambient);
    const checkbox = qs('[data-ambient-toggle]');
    if (checkbox) checkbox.checked = state.ambient;
  };

  const setOnboardedView = () => {
    loginView.hidden = state.onboarded;
    appView.hidden = !state.onboarded;
    if (state.onboarded) window.requestAnimationFrame(() => appMain.focus({ preventScroll: true }));
  };

  const showToast = (message, actionLabel = '', action = null) => {
    window.clearTimeout(toastTimer);
    toastText.textContent = message;
    undoAction = action;
    toastAction.hidden = !actionLabel;
    toastAction.textContent = actionLabel;
    toast.hidden = false;
    toastTimer = window.setTimeout(() => {
      toast.hidden = true;
      undoAction = null;
    }, 4200);
  };

  toastAction?.addEventListener('click', () => {
    undoAction?.();
    toast.hidden = true;
    undoAction = null;
  });

  const updateTaskCount = () => {
    const count = state.tasks.filter((task) => !task.complete).length;
    qsa('[data-task-count]').forEach((element) => { element.textContent = String(count); });
  };

  const renderTasks = () => {
    const list = qs('[data-task-list]');
    const empty = qs('[data-task-empty]');
    if (!list || !empty) return;
    list.innerHTML = state.tasks.map((task) => `
      <article class="task-item ${task.complete ? 'is-complete' : ''}" data-task-id="${task.id}">
        <button class="task-check" type="button" aria-label="${task.complete ? 'Markeer als niet voltooid' : 'Markeer als voltooid'}" data-toggle-task>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>
        </button>
        <strong>${escapeHTML(task.title)}</strong>
        <button class="task-delete" type="button" aria-label="Verwijder taak" data-delete-task>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"/></svg>
        </button>
      </article>`).join('');
    empty.hidden = state.tasks.length > 0;
    list.hidden = state.tasks.length === 0;
    updateTaskCount();

    qsa('[data-toggle-task]', list).forEach((button) => {
      button.addEventListener('click', async () => {
        const id = Number(button.closest('[data-task-id]').dataset.taskId);
        try { await api('/api/todos/done', { method: 'POST', body: JSON.stringify({ id }) }); } catch (e) {}
        await loadTasks();
        showToast('Taak afgerond.');
      });
    });
    qsa('[data-delete-task]', list).forEach((button) => {
      button.addEventListener('click', async () => {
        const id = Number(button.closest('[data-task-id]').dataset.taskId);
        const task = state.tasks.find((item) => item.id === id);
        const title = task ? task.title : '';
        try { await api('/api/todos/delete', { method: 'POST', body: JSON.stringify({ id }) }); } catch (e) {}
        await loadTasks();
        showToast('Taak verwijderd.', 'Ongedaan maken', async () => {
          if (!title) return;
          try { await api('/api/todos/add', { method: 'POST', body: JSON.stringify({ text: title }) }); } catch (e) {}
          await loadTasks();
        });
      });
    });
  };

  const loadTasks = async () => {
    try {
      const todos = (await (await api('/api/todos')).json()).items || [];
      state.tasks = todos.map((t) => ({ id: t.id, title: t.text || '', complete: false }));
    } catch (e) {}
    renderTasks();
  };

  // --- Diensten / Google-koppeling --------------------------------------------
  const connectGoogle = async () => {
    try {
      const d = await (await api('/oauth/start')).json();
      if (d && d.url) { window.location.href = d.url; return; }
    } catch (e) {}
    showToast('Koppelen lukte niet. Probeer het zo nog eens.');
  };
  const loadIntegrations = async () => {
    let d = {};
    try { d = await (await api('/api/integrations')).json(); } catch (e) {}
    const connectRow = qs('[data-connect-google]');
    if (connectRow) connectRow.hidden = !!d.google;
    qsa('[data-svc]').forEach((row) => {
      const on = row.dataset.svc === 'openai' ? !!d.openai : !!d.google;
      const label = qs('[data-svc-label]', row);
      if (label) {
        label.textContent = on ? 'Actief' : 'Niet gekoppeld';
        label.classList.toggle('active-label', on);
        label.classList.toggle('setting-value', !on);
      }
    });
  };

  // --- Tekstgrootte (client-side via zoom) ------------------------------------
  const applyTextScale = (size) => {
    const s = ['normal', 'groot', 'xl'].includes(size) ? size : 'normal';
    root.dataset.text = s;
    qsa('[data-text-seg] button').forEach((b) => b.classList.toggle('on', b.dataset.textSize === s));
    try { localStorage.setItem('able-textsize', s); } catch (e) {}
  };

  // --- Taal (server-side: stuurt spraakherkenning + antwoorden) ---------------
  const applyLangUI = (lang) => {
    const l = lang === 'en' ? 'en' : 'nl';
    qsa('[data-lang-seg] button').forEach((b) => b.classList.toggle('on', b.dataset.lang === l));
  };
  const setLang = async (lang) => {
    applyLangUI(lang);
    try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ lang }) }); } catch (e) {}
    showToast(lang === 'en' ? 'Language set to English.' : 'Taal ingesteld op Nederlands.');
  };

  // --- Spraakbudget -----------------------------------------------------------
  const loadVoiceBudget = async () => {
    let d = {};
    try { d = await (await api('/api/voice-usage')).json(); } catch (e) {}
    const pct = Math.max(0, Math.min(100, Number(d.pct) || 0));
    const fill = qs('[data-budget-fill]');
    if (fill) {
      fill.style.width = pct + '%';
      fill.classList.toggle('amber', pct >= 60 && pct < 90);
      fill.classList.toggle('red', pct >= 90);
    }
    const eur = (c) => '€' + ((Number(c) || 0) / 100).toFixed(2).replace('.', ',');
    setText('[data-budget-text]', `${eur(d.spent_cents)} / ${eur(d.cap_cents)}`);
  };

  // --- Gebruikersbeheer (alleen admin) ----------------------------------------
  const loadUsers = async () => {
    const group = qs('[data-admin-users]');
    if (!group) return;
    if (!currentUser || !currentUser.is_admin) { group.hidden = true; return; }
    group.hidden = false;
    let users = [];
    try { users = (await (await api('/api/users')).json()).users || []; } catch (e) {}
    const list = qs('[data-users-list]');
    if (!list) return;
    list.innerHTML = users.map((u) => {
      const tag = u.is_admin ? 'Beheerder' : (u.has_google ? 'Google gekoppeld' : 'Nog geen Google');
      const del = u.is_admin ? '' : `<button class="udel" data-del-user="${u.id}" aria-label="Verwijder ${escapeHTML(u.name)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13"/></svg></button>`;
      return `<div class="userrow"><span class="uava">${escapeHTML((u.name || '?').slice(0, 1).toUpperCase())}</span><span><strong>${escapeHTML(u.name)}</strong><small class="upill">@${escapeHTML(u.username)} · ${tag}</small></span>${del}</div>`;
    }).join('');
    qsa('[data-del-user]', list).forEach((btn) => btn.addEventListener('click', async () => {
      const id = Number(btn.dataset.delUser);
      const u = users.find((x) => x.id === id);
      if (!window.confirm(`${u ? u.name : 'Deze gebruiker'} en al hun gegevens definitief verwijderen?`)) return;
      try { await api('/api/users/delete', { method: 'POST', body: JSON.stringify({ id }) }); } catch (e) {}
      await loadUsers();
      showToast('Gebruiker verwijderd.');
    }));
  };

  const loadMore = () => {
    loadIntegrations();
    loadVoiceBudget();
    loadUsers();
    applyAvatar();
    const pn = qs('[data-profile-name]');
    if (pn && currentUser) pn.value = currentUser.name || '';
    api('/api/settings').then((r) => r.json()).then((s) => applyLangUI(s.lang || 'nl')).catch(() => {});
  };

  // --- Mail -------------------------------------------------------------------
  const senderName = (s) => {
    s = (s || '').trim();
    const lt = s.indexOf('<');
    if (lt > 0) return (s.slice(0, lt).replace(/["']/g, '').trim()) || s.slice(lt + 1).replace('>', '');
    if (s.includes('@')) return s.split('@')[0];
    return s || 'Onbekend';
  };
  const mailWhen = (iso) => {
    if (!iso) return '';
    const d = new Date(iso); const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });
    const days = Math.round((now - d) / 86400000);
    if (days <= 1) return 'gisteren';
    if (days < 7) return `${days} dagen`;
    return d.toLocaleDateString('nl-NL', { day: 'numeric', month: 'short' });
  };
  const loadMail = async () => {
    const body = qs('[data-mail-body]');
    if (!body) return;
    let d = { items: [], supported: true };
    try { d = await (await api('/api/mail')).json(); } catch (e) {}
    if (!d.supported) {
      body.innerHTML = '<div class="empty-state"><span><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m4 7 8 6 8-6"/></svg></span><h2>Mail-lezen komt eraan.</h2><p>Voor dit account is nog geen mailbox om te lezen gekoppeld.</p></div>';
      return;
    }
    const items = d.items || [];
    if (!items.length) {
      body.innerHTML = '<div class="empty-state"><span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg></span><h2>Inbox is rustig.</h2><p>Geen recente mail.</p></div>';
      return;
    }
    const av = (s) => escapeHTML(senderName(s).slice(0, 1).toUpperCase());
    const first = items[0];
    const rest = items.slice(1);
    let html = `<article class="mail-priority-card hover-card">
      <div class="mail-priority-top"><span class="avatar">${av(first.sender)}</span><span><strong>${escapeHTML(senderName(first.sender))}</strong><small>${escapeHTML(mailWhen(first.date))}</small></span><span class="status-pill">Nieuwste</span></div>
      <h2>${escapeHTML(first.subject)}</h2>
      <button class="primary-button" type="button" data-mail-open="0"><span>Bespreek met Able</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button>
    </article>`;
    if (rest.length) {
      html += '<div class="list-card mail-list">' + rest.map((m, i) => `
        <button class="list-row press-card list-appear" type="button" data-mail-open="${i + 1}"><span class="avatar">${av(m.sender)}</span><span><strong>${escapeHTML(senderName(m.sender))}</strong><small>${escapeHTML(m.subject)} · ${escapeHTML(mailWhen(m.date))}</small></span><svg class="chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button>`).join('') + '</div>';
    }
    body.innerHTML = html;
    qsa('[data-mail-open]', body).forEach((btn) => btn.addEventListener('click', () => {
      const m = items[Number(btn.dataset.mailOpen)];
      if (!m) return;
      openChat();
      const input = qs('[data-chat-form] input');
      if (input) { input.value = `Help me met de mail van ${senderName(m.sender)} over "${m.subject}".`; input.focus(); }
    }));
  };

  // --- Planning: echte agenda-tijdlijn + week-strip ---------------------------
  const WD_SHORT = ['Zo', 'Ma', 'Di', 'Wo', 'Do', 'Vr', 'Za'];
  const dateKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  let planningSelectedKey = null;

  const renderTimeline = (byDay, key) => {
    const tl = qs('.timeline');
    if (!tl) return;
    const events = (byDay[key] || []).slice().sort((a, b) => (a.time || '').localeCompare(b.time || ''));
    if (!events.length) {
      tl.innerHTML = '<div class="empty-state"><span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg></span><h2>Niets gepland.</h2><p>Een rustige dag.</p></div>';
      return;
    }
    const nowMs = Date.now();
    const nextIdx = events.findIndex((e) => e.iso && new Date(e.iso).getTime() >= nowMs);
    tl.innerHTML = events.map((e, i) => `
      <article class="timeline-item list-appear${i === nextIdx ? ' is-next' : ''}">
        <time>${escapeHTML(e.time || '')}</time><i></i>
        <div><strong>${escapeHTML(e.title || '')}</strong>${e.allday ? '<small>hele dag</small>' : ''}</div>
      </article>`).join('');
  };

  const loadPlanning = async () => {
    let agenda = [];
    try { agenda = (await (await api('/api/agenda')).json()).items || []; } catch (e) {}
    const byDay = {};
    agenda.forEach((e) => { if (e.iso) { const k = dateKey(new Date(e.iso)); (byDay[k] = byDay[k] || []).push(e); } });

    const today = new Date(); today.setHours(0, 0, 0, 0);
    const monday = new Date(today); monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
    const days = [];
    for (let i = 0; i < 7; i++) { const d = new Date(monday); d.setDate(monday.getDate() + i); days.push(d); }
    const todayKey = dateKey(today);
    if (!planningSelectedKey || !days.some((d) => dateKey(d) === planningSelectedKey)) planningSelectedKey = todayKey;

    const strip = qs('.date-strip');
    if (strip) {
      const slider = qs('.date-slider', strip);
      strip.innerHTML = '';
      if (slider) strip.appendChild(slider);
      days.forEach((d, index) => {
        const k = dateKey(d);
        const btn = document.createElement('button');
        btn.type = 'button';
        if (byDay[k]) btn.classList.add('has-events');
        if (k === todayKey) btn.classList.add('is-today');
        btn.setAttribute('aria-pressed', String(k === planningSelectedKey));
        btn.innerHTML = `<span>${WD_SHORT[d.getDay()]}</span><strong>${d.getDate()}</strong><i aria-hidden="true"></i>`;
        btn.addEventListener('click', () => {
          planningSelectedKey = k;
          qsa('button', strip).forEach((b) => b.setAttribute('aria-pressed', 'false'));
          btn.setAttribute('aria-pressed', 'true');
          strip.style.setProperty('--day-offset', `calc(${index * 100}% + ${index * 3}px)`);
          renderTimeline(byDay, k);
        });
        strip.appendChild(btn);
      });
      const selIdx = days.findIndex((d) => dateKey(d) === planningSelectedKey);
      strip.style.setProperty('--day-offset', `calc(${selIdx * 100}% + ${selIdx * 3}px)`);
    }
    renderTimeline(byDay, planningSelectedKey);
  };

  const planningTab = (tab) => {
    qsa('[data-planning-tab]').forEach((button) => button.setAttribute('aria-selected', String(button.dataset.planningTab === tab)));
    qsa('[data-planning-panel]').forEach((panel) => { panel.hidden = panel.dataset.planningPanel !== tab; });
    const segmentedControl = qs('.segmented-control');
    segmentedControl?.style.setProperty('--planning-index', tab === 'tasks' ? '1' : '0');
    if (segmentedControl) segmentedControl.dataset.activeTab = tab;
    if (tab === 'tasks') qs('[data-task-form] input')?.focus();
  };

  const resetPull = () => {
    pullStartY = null;
    pullDistance = 0;
    appView.classList.remove('is-pulling');
    appView.style.setProperty('--pull-distance', '0px');
  };

  const runRefresh = () => {
    if (isRefreshing || !state.onboarded) return;
    isRefreshing = true;
    resetPull();
    appView.classList.add('is-refreshing');
    loadingOverlay.classList.remove('is-resolving');
    loadingOverlay.hidden = false;

    window.setTimeout(() => {
      loadHome();
      loadingOverlay.classList.add('is-resolving');
      body.classList.add('is-resolving');
      window.setTimeout(() => {
        loadingOverlay.hidden = true;
        loadingOverlay.classList.remove('is-resolving');
        appView.classList.remove('is-refreshing');
        body.classList.remove('is-resolving');
        isRefreshing = false;
        showToast('Bijgewerkt.');
      }, 430);
    }, 720);
  };

  const navigate = (target, moveFocus = true, updateHistory = true) => {
    if (!state.onboarded || !qs(`[data-screen="${target}"]`)) return;
    const current = qs('[data-screen].is-active');
    if (current?.dataset.screen === target) return;
    if (activeScreen === 'voice' && target !== 'voice') stopVoice();
    current?.classList.add('is-leaving');
    window.setTimeout(() => {
      qsa('[data-screen]').forEach((screen) => {
        const selected = screen.dataset.screen === target;
        screen.hidden = !selected;
        screen.classList.toggle('is-active', selected);
        screen.classList.remove('is-leaving');
      });
      qsa(`[data-screen="${target}"] .list-appear`).forEach((element) => {
        element.classList.remove('list-appear');
        void element.offsetWidth;
        element.classList.add('list-appear');
      });
      activeScreen = target;
      if (target === 'planning') { loadPlanning(); loadTasks(); }
      if (target === 'more') loadMore();
      if (target === 'mail') loadMail();
      appView.classList.toggle('voice-active', target === 'voice');
      const navOrder = ['home', 'planning', 'voice', 'mail', 'more'];
      root.style.setProperty('--nav-index', String(navOrder.indexOf(target)));
      qsa('[data-bottom-nav] [data-nav-target]').forEach((button) => {
        const selected = button.dataset.navTarget === target;
        button.classList.toggle('is-selected', selected);
        if (selected) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      if (updateHistory && window.location.hash !== `#${target}`) {
        window.history.pushState({ screen: target }, '', `#${target}`);
      }
      appMain.scrollTo({ top: 0, behavior: 'auto' });
      if (moveFocus) appMain.focus({ preventScroll: true });
    }, current ? 130 : 0);
  };

  qsa('[data-nav-target]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (button.closest('[data-bottom-nav]') && button.dataset.navTarget === activeScreen) {
        if (activeScreen === 'home') {
          appMain.scrollTo({ top: 0, behavior: 'smooth' });
          runRefresh();
        } else if (activeScreen === 'voice') {
          startVoiceSequence();
        }
        return;
      }
      navigate(button.dataset.navTarget);
      if (button.dataset.navTarget === 'voice') {
        window.setTimeout(startVoiceSequence, 180);
      }
    });
  });

  appMain?.addEventListener('touchstart', (event) => {
    if (activeScreen !== 'home' || isRefreshing || appMain.scrollTop > 0 || event.touches.length !== 1) return;
    pullStartY = event.touches[0].clientY;
  }, { passive: true });
  appMain?.addEventListener('touchmove', (event) => {
    if (pullStartY === null || appMain.scrollTop > 0) return;
    const delta = event.touches[0].clientY - pullStartY;
    if (delta <= 0) {
      resetPull();
      return;
    }
    pullDistance = Math.min(72, delta * .55);
    appView.classList.add('is-pulling');
    appView.style.setProperty('--pull-distance', `${pullDistance}px`);
  }, { passive: true });
  const finishPull = () => {
    if (pullStartY === null) return;
    if (pullDistance >= 52) runRefresh();
    else resetPull();
  };
  appMain?.addEventListener('touchend', finishPull, { passive: true });
  appMain?.addEventListener('touchcancel', resetPull, { passive: true });

  qs('[data-login-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = qs('button[type="submit"]', form);
    const username = (qs('input[type="text"]', form).value || '').trim().toLowerCase();
    const password = qs('input[type="password"]', form).value || '';
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span>Even voorbereiden…</span>';
    clearLoginError();
    try {
      const res = await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      if (!res.ok) throw new Error('auth');
      let me = { auth: true, name: '' };
      try { me = await (await api('/api/me')).json(); } catch (e) {}
      applyUser(me);
      loadHome();
      state.onboarded = true;
      save();
      body.classList.add('is-resolving');
      setOnboardedView();
      window.history.replaceState({ screen: 'home' }, '', '#home');
      navigate('home', false, false);
      window.setTimeout(() => body.classList.remove('is-resolving'), 720);
    } catch (err) {
      button.disabled = false;
      button.innerHTML = original;
      showLoginError('Onjuiste naam of wachtwoord.');
    }
  });

  qs('[data-theme-toggle]')?.addEventListener('click', () => applyTheme(state.theme === 'forest' ? 'light' : 'forest'));
  qs('[data-setting-theme]')?.addEventListener('click', () => {
    applyTheme(state.theme === 'forest' ? 'light' : 'forest');
    showToast(`Thema gewijzigd naar ${state.theme === 'forest' ? 'Able Forest' : 'Able Light'}.`);
  });
  qs('[data-ambient-toggle]')?.addEventListener('change', (event) => {
    state.ambient = event.currentTarget.checked;
    applyAmbient();
    save();
    showToast(state.ambient ? 'Ambient glow staat aan.' : 'Ambient glow staat uit.');
  });

  qsa('[data-planning-tab]').forEach((button) => button.addEventListener('click', () => planningTab(button.dataset.planningTab)));
  const dateStrip = qs('.date-strip');
  const dateButtons = qsa('.date-strip button');
  dateButtons.forEach((button, index) => {
    button.addEventListener('click', () => {
      dateButtons.forEach((item) => {
        item.classList.remove('is-today');
        item.setAttribute('aria-pressed', 'false');
      });
      button.classList.add('is-today');
      button.setAttribute('aria-pressed', 'true');
      dateStrip?.style.setProperty('--day-offset', `calc(${index * 100}% + ${index * 3}px)`);
      showToast(`${button.querySelector('span').textContent} ${button.querySelector('strong').textContent} geselecteerd.`);
    });
  });
  qsa('[data-show-tasks]').forEach((button) => button.addEventListener('click', () => {
    navigate('planning');
    window.setTimeout(() => planningTab('tasks'), 150);
  }));

  qs('[data-task-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = qs('input', event.currentTarget);
    const title = input.value.trim();
    if (!title) return;
    input.value = '';
    try { await api('/api/todos/add', { method: 'POST', body: JSON.stringify({ text: title }) }); } catch (e) {}
    await loadTasks();
    showToast('Taak toegevoegd.');
  });

  const protectFocus = () => {
    if (state.focusProtected) {
      showToast('Het uur om 15:00 is al beschermd.');
      navigate('planning');
      return;
    }
    state.focusProtected = true;
    qs('[data-focus-block]').hidden = false;
    save();
    body.classList.add('is-resolving');
    window.setTimeout(() => body.classList.remove('is-resolving'), 720);
    showToast('15:00–16:00 is beschermd in je planning.');
  };
  qs('[data-protect-focus]')?.addEventListener('click', protectFocus);

  const openSheet = (content) => {
    lastFocusedElement = document.activeElement;
    sheetContent.innerHTML = content;
    sheetBackdrop.hidden = false;
    body.style.overflow = 'hidden';
    qs('[data-close-sheet]')?.focus();
  };
  const closeSheet = () => {
    sheetBackdrop.hidden = true;
    body.style.overflow = '';
    lastFocusedElement?.focus?.();
  };
  qs('[data-close-sheet]')?.addEventListener('click', closeSheet);
  sheetBackdrop?.addEventListener('click', (event) => { if (event.target === sheetBackdrop) closeSheet(); });

  qsa('[data-open-event]').forEach((button) => button.addEventListener('click', () => {
    openSheet(`
      <div class="sheet-content">
        <p class="sheet-meta">Vandaag · 14:00–14:45</p>
        <h2 id="sheet-title">Afspraak met Arno</h2>
        <p>Kantoor · vergaderruimte Victoria</p>
        <div class="suggested-reply"><strong>Able heeft voorbereid</strong><br>Bespreek de nieuwe interface, de planning voor augustus en de openstaande feedback van Maya.</div>
        <div class="sheet-actions"><button class="primary-button" type="button" data-sheet-protect>Bescherm het uur erna</button><button class="quiet-button" type="button" data-close-sheet-inline>Sluiten</button></div>
      </div>`);
    qs('[data-sheet-protect]')?.addEventListener('click', () => { protectFocus(); closeSheet(); });
    qs('[data-close-sheet-inline]')?.addEventListener('click', closeSheet);
  }));

  const mailData = {
    maya: { meta: 'Maya Koster · 2 dagen geleden', title: 'Kunnen we een datum voor augustus vastleggen?', body: 'Ik heb dinsdagmiddag en donderdag de hele dag ruimte. Laat maar weten wat voor jou het beste werkt.', reply: 'Donderdag past goed. Zullen we 14:00 aanhouden? Dan stuur ik meteen een uitnodiging.' },
    teun: { meta: 'Teun Kensen · 1 dag geleden', title: 'Fifa', body: 'Heb je vanavond nog zin in een potje?', reply: 'Ja, ik kan na 20:30. Ik stuur je een bericht zodra ik klaar ben.' },
    peer: { meta: 'Peer Noordermeer · 1 dag geleden', title: 'Slaap lekker', body: 'Dankjewel voor vandaag. Spreek je morgen.', reply: 'Jij ook, slaap lekker. Tot morgen!' },
    arno: { meta: 'Arno Kensen · vandaag', title: 'De nieuwe versie werkt heel goed', body: 'De flow voelt een stuk duidelijker. Ik heb nog twee kleine punten voor vanmiddag.', reply: 'Mooi om te horen. Neem de twee punten straks vooral mee, dan lopen we ze samen langs.' }
  };
  qsa('[data-open-mail]').forEach((button) => button.addEventListener('click', () => {
    const mail = mailData[button.dataset.openMail];
    if (!mail) return;
    openSheet(`
      <div class="sheet-content">
        <p class="sheet-meta">${escapeHTML(mail.meta)}</p>
        <h2 id="sheet-title">${escapeHTML(mail.title)}</h2>
        <p>${escapeHTML(mail.body)}</p>
        <p class="sheet-meta">Voorgesteld antwoord</p>
        <div class="suggested-reply">${escapeHTML(mail.reply)}</div>
        <div class="sheet-actions"><button class="primary-button" type="button" data-send-reply>Verstuur antwoord</button><button class="quiet-button" type="button" data-close-sheet-inline>Later</button></div>
      </div>`);
    qs('[data-send-reply]')?.addEventListener('click', (event) => {
      const replyButton = event.currentTarget;
      replyButton.disabled = true;
      replyButton.textContent = 'Wordt verstuurd…';
      window.setTimeout(() => {
        body.classList.add('is-resolving');
        closeSheet();
        showToast('Antwoord verstuurd.');
        window.setTimeout(() => body.classList.remove('is-resolving'), 720);
      }, 650);
    });
    qs('[data-close-sheet-inline]')?.addEventListener('click', closeSheet);
  }));

  qsa('[data-service]').forEach((button) => button.addEventListener('click', () => {
    const service = button.dataset.service;
    openSheet(`
      <div class="sheet-content">
        <p class="sheet-meta">Verbonden</p>
        <h2 id="sheet-title">${escapeHTML(service)}</h2>
        <div class="suggested-reply">Context lezen · acties na jouw bevestiging.</div>
        <div class="sheet-actions"><button class="primary-button" type="button" data-close-sheet-inline>Gereed</button></div>
      </div>`);
    qs('[data-close-sheet-inline]')?.addEventListener('click', closeSheet);
  }));

  const openChat = (event) => {
    event?.preventDefault();
    stopVoice();
    chatOverlay.hidden = false;
    chatOverlay.setAttribute('aria-hidden', 'false');
    body.style.overflow = 'hidden';
    window.requestAnimationFrame(() => qs('[data-chat-form] input')?.focus());
  };
  const closeChat = () => {
    chatOverlay.hidden = true;
    chatOverlay.setAttribute('aria-hidden', 'true');
    body.style.overflow = '';
    qs('[data-open-chat]')?.focus();
  };
  qsa('[data-open-chat]').forEach((button) => button.addEventListener('click', (event) => openChat(event)));
  qs('[data-close-chat]')?.addEventListener('click', closeChat);
  const chatForm = qs('[data-chat-form]');
  let chatBusy = false;
  const sendChatMessage = async (message) => {
    if (!message || chatBusy) return;
    chatBusy = true;
    const thread = qs('[data-chat-thread]');
    qs('.chat-empty', thread)?.remove();
    thread.insertAdjacentHTML('beforeend', `<div class="chat-bubble user">${escapeHTML(message)}</div>`);
    thread.insertAdjacentHTML('beforeend', '<div class="chat-thinking" data-chat-thinking role="status" aria-label="Able denkt"><span></span><span></span><span></span></div>');
    thread.scrollTop = thread.scrollHeight;

    let bubble = null;
    const ensureBubble = () => {
      qs('[data-chat-thinking]', thread)?.remove();
      if (!bubble) {
        thread.insertAdjacentHTML('beforeend', '<div class="chat-bubble able"></div>');
        bubble = thread.lastElementChild;
      }
      return bubble;
    };
    try {
      const res = await api('/api/chat/stream', { method: 'POST', body: JSON.stringify({ message }) });
      if (!res.ok || !res.body) throw new Error('stream');
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (!text) continue;
        acc += text;
        ensureBubble().textContent = acc;
        thread.scrollTop = thread.scrollHeight;
      }
      if (!bubble) ensureBubble().textContent = 'Genoteerd.';
    } catch (e) {
      try {
        const r = await api('/api/chat', { method: 'POST', body: JSON.stringify({ message }) });
        const d = await r.json();
        ensureBubble().textContent = d.reply || 'Sorry, daar ging iets mis. Probeer het zo nog eens?';
      } catch (e2) {
        ensureBubble().textContent = 'Sorry, daar ging iets mis. Probeer het zo nog eens?';
      }
    } finally {
      qs('[data-chat-thinking]', thread)?.remove();
      thread.scrollTop = thread.scrollHeight;
      chatBusy = false;
    }
  };
  chatForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    const input = qs('input', event.currentTarget);
    const message = input.value.trim();
    input.value = '';
    sendChatMessage(message);
  });
  qsa('[data-chat-suggestion]').forEach((button) => button.addEventListener('click', () => sendChatMessage(button.textContent.trim())));
  qs('[data-chat-voice]')?.addEventListener('click', () => {
    closeChat();
    navigate('voice');
    window.setTimeout(startVoiceSequence, 180);
  });

  const clearVoiceTimers = () => {
    voiceTimers.forEach((timer) => window.clearTimeout(timer));
    voiceTimers = [];
  };
  // --- Voice: echte WebRTC realtime (OpenAI) ----------------------------------
  let vpc = null, vdc = null, micStream = null, voiceOn = false, micMuted = false;
  let ableText = '', voiceStart = 0, voiceUsage = null;

  const setVoiceState = (voiceState, statusOverride) => {
    const voiceCopy = qs('[data-voice-copy]');
    const statusText = qs('[data-voice-status-text]');
    const spoken = qs('[data-voice-spoken]');
    const edge = qs('[data-voice-edge]');
    const controlLabel = qs('[data-voice-control] span');
    ['idle', 'listening', 'thinking', 'speaking'].forEach((name) => {
      if (voiceCopy) voiceCopy.classList.remove(`state-${name}`);
      if (edge) edge.classList.remove(`state-${name}`);
    });
    if (voiceCopy) voiceCopy.classList.add(`state-${voiceState}`);
    if (edge) edge.classList.add(`state-${voiceState}`);
    const screen = qs('[data-screen="voice"]');
    if (screen) screen.dataset.voiceState = voiceState;
    const copy = {
      idle: ['Klaar wanneer jij dat bent.', 'Praat'],
      listening: ['Ik luister…', 'Stop'],
      thinking: ['Momentje…', 'Denkt'],
      speaking: ['', 'Onderbreek']
    }[voiceState] || ['', 'Praat'];
    if (statusText) statusText.textContent = statusOverride != null ? statusOverride : copy[0];
    if (controlLabel) controlLabel.textContent = copy[1];
    if (statusText) statusText.hidden = voiceState === 'speaking';
    if (spoken) {
      spoken.hidden = voiceState !== 'speaking';
      if (voiceState !== 'speaking') { spoken.textContent = ''; ableText = ''; }
    }
  };
  const setSpoken = (text) => {
    const spoken = qs('[data-voice-spoken]');
    if (spoken) { spoken.textContent = text; spoken.scrollTop = spoken.scrollHeight; }
  };
  const remoteAudio = () => {
    let a = document.getElementById('able-remote-audio');
    if (!a) {
      a = document.createElement('audio');
      a.id = 'able-remote-audio';
      a.autoplay = true; a.setAttribute('playsinline', '');
      document.body.appendChild(a);
    }
    return a;
  };
  const setMicOn = (on) => { try { if (micStream) micStream.getAudioTracks()[0].enabled = on; } catch (e) {} };
  const sendVoiceEvt = (o) => { try { if (vdc && vdc.readyState === 'open') vdc.send(JSON.stringify(o)); } catch (e) {} };
  const reportVoiceSeconds = () => {
    if (!voiceStart) return;
    const secs = Math.round((Date.now() - voiceStart) / 1000);
    voiceStart = 0;
    if (secs <= 0) return;
    api('/api/voice-usage', { method: 'POST', body: JSON.stringify({ seconds: secs }) })
      .then((r) => r.json()).then((d) => { voiceUsage = d; }).catch(() => {});
  };
  const runVoiceTool = (name, callId, argsStr) => {
    api('/api/tool', { method: 'POST', body: JSON.stringify({ name, arguments: argsStr }) })
      .then((r) => r.json())
      .then((d) => {
        sendVoiceEvt({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: (d && d.output) || 'ok' } });
        sendVoiceEvt({ type: 'response.create' });
      }).catch(() => {
        sendVoiceEvt({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: 'Fout bij uitvoeren.' } });
        sendVoiceEvt({ type: 'response.create' });
      });
  };
  const onVoiceEvt = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    const et = m.type || '';
    if (et.indexOf('speech_started') >= 0) { ableText = ''; setVoiceState('listening'); }
    else if (et.indexOf('speech_stopped') >= 0) setVoiceState('thinking');
    else if (et === 'output_audio_buffer.started') { setMicOn(false); setVoiceState('speaking'); }
    else if (et === 'output_audio_buffer.stopped' || et === 'output_audio_buffer.cleared') {
      window.setTimeout(() => setMicOn(!micMuted), 300);
      setVoiceState('listening');
    } else if (et.indexOf('audio_transcript.delta') >= 0) {
      setVoiceState('speaking');
      ableText += (m.delta || '');
      setSpoken(ableText);
    } else if (et === 'response.done') {
      const out = (m.response && m.response.output) || [];
      out.forEach((item) => { if (item.type === 'function_call') runVoiceTool(item.name, item.call_id, item.arguments); });
    }
  };
  const startVoiceSequence = async () => {
    if (voiceOn || vpc) return;
    setVoiceState('idle', 'Even klaarzetten…');
    try {
      voiceUsage = await (await api('/api/voice-usage')).json();
      if (voiceUsage && (voiceUsage.remaining_cents || 0) <= 0) { setVoiceState('idle', 'Je spraakbudget voor deze maand is op.'); return; }
    } catch (e) {}
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) { setVoiceState('idle', 'Geen toegang tot de microfoon.'); return; }
    let sres;
    try { sres = await api('/api/realtime-session', { method: 'POST' }); }
    catch (e) { setVoiceState('idle', 'Kon de spraakverbinding niet starten.'); return; }
    if (!sres.ok) { setVoiceState('idle', sres.status === 402 ? 'Je spraakbudget is op.' : 'Starten lukte niet.'); return; }
    const data = await sres.json();
    const eph = data.value;
    if (!eph) { setVoiceState('idle', 'Spraak is nog niet ingesteld op de server.'); return; }
    try {
      const audio = remoteAudio();
      vpc = new RTCPeerConnection();
      vpc.ontrack = (e) => { audio.srcObject = e.streams[0]; if (audio.play) audio.play().catch(() => {}); };
      vpc.addTrack(micStream.getTracks()[0], micStream);
      vdc = vpc.createDataChannel('oai-events');
      vdc.onmessage = onVoiceEvt;
      vdc.onopen = () => { voiceOn = true; voiceStart = Date.now(); setVoiceState('listening'); };
      const offer = await vpc.createOffer();
      await vpc.setLocalDescription(offer);
      const sdpRes = await fetch('https://api.openai.com/v1/realtime/calls?model=' + encodeURIComponent(data.model), {
        method: 'POST', body: offer.sdp, headers: { Authorization: 'Bearer ' + eph, 'Content-Type': 'application/sdp' } });
      if (!sdpRes.ok) { setVoiceState('idle', 'Verbinding geweigerd.'); return; }
      await vpc.setRemoteDescription({ type: 'answer', sdp: await sdpRes.text() });
    } catch (e) { setVoiceState('idle', 'Verbinding mislukt.'); }
  };
  const stopVoice = () => {
    reportVoiceSeconds();
    voiceOn = false; micMuted = false;
    try { if (vdc) vdc.close(); } catch (e) {}
    try { if (vpc) vpc.close(); } catch (e) {}
    try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    vpc = null; vdc = null; micStream = null; ableText = '';
    setVoiceState('idle');
  };
  qsa('[data-voice-control]').forEach((button) => button.addEventListener('click', () => {
    if (!voiceOn) { startVoiceSequence(); return; }
    const st = qs('[data-screen="voice"]').dataset.voiceState;
    if (st === 'speaking') {
      sendVoiceEvt({ type: 'response.cancel' });
      sendVoiceEvt({ type: 'output_audio_buffer.clear' });
      setMicOn(!micMuted);
      setVoiceState('listening');
      return;
    }
    stopVoice();
  }));
  qsa('[data-stop-voice]').forEach((button) => button.addEventListener('click', () => {
    stopVoice();
    navigate('home');
  }));

  qsa('[data-toast-message]').forEach((button) => button.addEventListener('click', () => showToast(button.dataset.toastMessage)));
  // --- Profiel: naam wijzigen + foto uit galerij ------------------------------
  const fileToAvatar = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const size = 256;
        const canvas = document.createElement('canvas');
        canvas.width = size; canvas.height = size;
        const ctx = canvas.getContext('2d');
        const scale = Math.max(size / img.width, size / img.height);
        const w = img.width * scale; const h = img.height * scale;
        ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
        resolve(canvas.toDataURL('image/jpeg', 0.85));
      };
      img.onerror = reject;
      img.src = reader.result;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  qsa('[data-avatar-pick]').forEach((b) => b.addEventListener('click', () => qs('[data-avatar-file]')?.click()));
  qs('[data-avatar-file]')?.addEventListener('change', async (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    try {
      const dataUrl = await fileToAvatar(file);
      const r = await api('/api/profile', { method: 'POST', body: JSON.stringify({ avatar: dataUrl }) });
      if (!r.ok) throw new Error('avatar');
      if (currentUser) currentUser.avatar = dataUrl;
      applyAvatar();
      showToast('Profielfoto bijgewerkt.');
    } catch (e) { showToast('Kon de foto niet instellen.'); }
    event.target.value = '';
  });
  qs('[data-avatar-remove]')?.addEventListener('click', async () => {
    try { await api('/api/profile', { method: 'POST', body: JSON.stringify({ avatar: null }) }); } catch (e) {}
    if (currentUser) currentUser.avatar = null;
    applyAvatar();
    showToast('Profielfoto verwijderd.');
  });
  qs('[data-profile-name-form]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = qs('input', event.currentTarget);
    const name = input.value.trim();
    if (!name) return;
    try {
      const r = await api('/api/profile', { method: 'POST', body: JSON.stringify({ name }) });
      if (!r.ok) throw new Error('name');
      if (currentUser) currentUser.name = name;
      applyUser(currentUser);
      showToast('Naam opgeslagen.');
    } catch (e) { showToast('Naam opslaan lukte niet.'); }
  });
  qs('[data-topbar-avatar]')?.addEventListener('click', () => navigate('more'));

  qs('[data-connect-google]')?.addEventListener('click', connectGoogle);
  qsa('[data-text-seg] button').forEach((b) => b.addEventListener('click', () => applyTextScale(b.dataset.textSize)));
  qsa('[data-lang-seg] button').forEach((b) => b.addEventListener('click', () => setLang(b.dataset.lang)));
  applyTextScale(localStorage.getItem('able-textsize') || 'normal');
  qs('[data-user-add]')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const f = event.currentTarget;
    const name = qs('input[name="name"]', f).value.trim();
    const username = qs('input[name="username"]', f).value.trim().toLowerCase();
    const password = qs('input[name="password"]', f).value;
    if (!name || !username || password.length < 4) { showToast('Vul naam, gebruikersnaam en wachtwoord (min. 4) in.'); return; }
    try {
      const r = await api('/api/users', { method: 'POST', body: JSON.stringify({ name, username, password }) });
      if (!r.ok) throw new Error('add');
      f.reset();
      await loadUsers();
      showToast(`${name} toegevoegd.`);
    } catch (e) { showToast('Toevoegen lukte niet (bestaat de gebruikersnaam al?).'); }
  });
  qs('[data-log-out]')?.addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST' }); } catch (e) {}
    currentUser = null;
    state.onboarded = false;
    save();
    setOnboardedView();
    window.history.replaceState({ screen: 'home' }, '', '#home');
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!chatOverlay.hidden) closeChat();
    else if (!sheetBackdrop.hidden) closeSheet();
  });
  window.addEventListener('popstate', () => {
    const target = window.location.hash.slice(1);
    if (['home', 'planning', 'voice', 'mail', 'more'].includes(target)) navigate(target, true, false);
  });

  if ('serviceWorker' in navigator && window.location.protocol.startsWith('http')) {
    window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js').catch(() => {}));
  }

  applyTheme(state.theme);
  applyAmbient();
  renderTasks();
  if (state.focusProtected) { const focusBlock = qs('[data-focus-block]'); if (focusBlock) focusBlock.hidden = false; }

  // Echte sessie: vraag de server wie er is ingelogd i.p.v. localStorage.
  const boot = async () => {
    if (isDemoMode) {
      applyUser({ auth: true, name: 'Ko' });
      state.onboarded = true;
      save();
      setOnboardedView();
      const initialTarget = window.location.hash.slice(1);
      if (['planning', 'voice', 'mail', 'more'].includes(initialTarget)) navigate(initialTarget, false, false);
      else window.history.replaceState({ screen: 'home' }, '', '#home');
      return;
    }
    let me = { auth: false };
    try { me = await (await api('/api/me')).json(); } catch (e) {}
    if (me && me.auth) { applyUser(me); loadHome(); state.onboarded = true; }
    else { state.onboarded = false; }
    save();
    setOnboardedView();
    const initialTarget = window.location.hash.slice(1);
    if (state.onboarded && ['home', 'planning', 'voice', 'mail', 'more'].includes(initialTarget)) navigate(initialTarget, false, false);
    else if (state.onboarded) window.history.replaceState({ screen: 'home' }, '', '#home');
  };
  boot();
})();
