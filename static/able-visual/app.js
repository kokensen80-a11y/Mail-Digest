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
      button.addEventListener('click', () => {
        const id = Number(button.closest('[data-task-id]').dataset.taskId);
        const task = state.tasks.find((item) => item.id === id);
        if (!task) return;
        task.complete = !task.complete;
        save();
        renderTasks();
        showToast(task.complete ? 'Taak afgerond.' : 'Taak opnieuw geopend.');
      });
    });
    qsa('[data-delete-task]', list).forEach((button) => {
      button.addEventListener('click', () => {
        const id = Number(button.closest('[data-task-id]').dataset.taskId);
        const index = state.tasks.findIndex((item) => item.id === id);
        if (index < 0) return;
        const [deleted] = state.tasks.splice(index, 1);
        save();
        renderTasks();
        showToast('Taak verwijderd.', 'Ongedaan maken', () => {
          state.tasks.splice(index, 0, deleted);
          save();
          renderTasks();
        });
      });
    });
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

  qs('[data-task-form]')?.addEventListener('submit', (event) => {
    event.preventDefault();
    const input = qs('input', event.currentTarget);
    const title = input.value.trim();
    if (!title) return;
    state.tasks.unshift({ id: Date.now(), title, complete: false });
    input.value = '';
    save();
    renderTasks();
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
  const sendChatMessage = (message) => {
    if (!message) return;
    const thread = qs('[data-chat-thread]');
    qs('.chat-empty', thread)?.remove();
    thread.insertAdjacentHTML('beforeend', `<div class="chat-bubble user">${escapeHTML(message)}</div>`);
    thread.insertAdjacentHTML('beforeend', '<div class="chat-thinking" data-chat-thinking role="status" aria-label="Able denkt"><span></span><span></span><span></span></div>');
    thread.scrollTop = thread.scrollHeight;
    window.setTimeout(() => {
      qs('[data-chat-thinking]', thread)?.remove();
      thread.insertAdjacentHTML('beforeend', '<div class="chat-bubble able">Je hebt om 15:00 een vrij uur. Ik kan dat beschermen voor je voorstel, of eerst je mail met Maya afronden.</div>');
      thread.scrollTop = thread.scrollHeight;
    }, 1600);
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
  const setVoiceState = (voiceState) => {
    const sphere = qs('[data-voice-sphere]');
    const title = qs('[data-voice-title]');
    const label = qs('[data-voice-label]');
    const transcript = qs('[data-voice-transcript]');
    const controlLabel = qs('[data-voice-sphere-control] span');
    ['idle', 'listening', 'thinking', 'speaking'].forEach((name) => sphere.classList.remove(`state-${name}`));
    sphere.classList.add(`state-${voiceState}`);
    const copy = {
      idle: ['Klaar wanneer jij dat bent.', 'Raak de sphere aan om te praten', '', 'Praat'],
      listening: ['Ik luister.', 'Vertel rustig wat je nodig hebt', '“Plan morgen een rustig uur voor het voorstel.”', 'Stop'],
      thinking: ['Eén moment.', 'Able ordent je verzoek', 'Ik bekijk je planning en open taken…', 'Denkt'],
      speaking: ['Dit kan rustig passen.', 'Able antwoordt', 'Morgen om 10:00 heb je een vrij uur. Ik kan dat als focusblok reserveren.', 'Praat']
    }[voiceState];
    [title.textContent, label.textContent, transcript.textContent, controlLabel.textContent] = copy;
    sphere.setAttribute('aria-label', voiceState === 'idle' ? 'Start luisteren' : 'Stop voice');
  };
  const startVoiceSequence = () => {
    clearVoiceTimers();
    setVoiceState('listening');
    voiceTimers.push(window.setTimeout(() => setVoiceState('thinking'), 2300));
    voiceTimers.push(window.setTimeout(() => setVoiceState('speaking'), 4200));
    voiceTimers.push(window.setTimeout(() => setVoiceState('idle'), 6900));
  };
  const stopVoice = () => {
    clearVoiceTimers();
    setVoiceState('idle');
  };
  qsa('[data-voice-sphere], [data-voice-sphere-control]').forEach((button) => button.addEventListener('click', () => {
    const sphere = qs('[data-voice-sphere]');
    if (sphere.classList.contains('state-idle')) startVoiceSequence();
    else stopVoice();
  }));
  qsa('[data-stop-voice]').forEach((button) => button.addEventListener('click', () => {
    stopVoice();
    navigate('home');
  }));

  qsa('[data-toast-message]').forEach((button) => button.addEventListener('click', () => showToast(button.dataset.toastMessage)));
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
    let me = { auth: false };
    try { me = await (await api('/api/me')).json(); } catch (e) {}
    if (me && me.auth) { applyUser(me); state.onboarded = true; }
    else { state.onboarded = false; }
    save();
    setOnboardedView();
    const initialTarget = window.location.hash.slice(1);
    if (state.onboarded && ['home', 'planning', 'voice', 'mail', 'more'].includes(initialTarget)) navigate(initialTarget, false, false);
    else if (state.onboarded) window.history.replaceState({ screen: 'home' }, '', '#home');
  };
  boot();
})();
