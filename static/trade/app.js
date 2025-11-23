// static/trade/app.js
(() => {
  // ---------- utils ----------

  function sideToIcon(side, choice){
    const raw = String(side || choice || '').toUpperCase();
    const isRain = (raw === 'PLUIE' || raw === 'RAIN' || raw === 'RAINY');
    return isRain ? '💧' : '☀️';
  }
  const $ = (sel, root=document) => root.querySelector(sel);
  const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

  function onOutsideClick(panel, closer) {
    function handler(e){
      if (!panel.contains(e.target) && e.target !== closer) {
        panel.hidden = true;
        document.removeEventListener('click', handler, true);
      }
    }
    document.addEventListener('click', handler, true);
  }

  function fmtPts(x){
    const n = Math.round((Number(x)||0)*100)/100;
    let s = n.toFixed(2).replace('.', ',');
    s = s.replace(/,0$/, '');
    return s;
  }

  function wrapGP(htmlOrText){
    if (typeof htmlOrText !== 'string') return htmlOrText;
    if (htmlOrText.includes('class="gp"')) return htmlOrText; // évite double wrap
    return htmlOrText.replace(/GP:\s*([\d.,]+)\s*pts/gi, '<span class="gp">GP: $1 pts</span>');
  }

  function colorizeGPIn(container=document){
    if (!container) return;
    container.querySelectorAll('.line, .sell-line, #selectedListingLabel').forEach(el=>{
      if (!el) return;
      const html = el.innerHTML;
      const wrapped = wrapGP(html);
      if (wrapped !== html) el.innerHTML = wrapped;
    });
  }

  // ---- heures (PPP) ----
  function extractHHmm(x){
    if (!x) return null;
    // "15:00" → 15:00
    if (/^\d{2}:\d{2}$/.test(x)) return x;
    // "2025-11-15T15:00" → 15:00
    const m = String(x).match(/T(\d{2}:\d{2})/);
    return m ? m[1] : null;
  }
  function hourLabelFrom(obj, fallback='18:00'){
    // essaye dans cet ordre : target_time, target_dt, payload.target_time, payload.target_dt
    const src =
      obj?.target_time || obj?.target_dt ||
      obj?.payload?.target_time || obj?.payload?.target_dt || '';

    const hhmm = extractHHmm(src) || fallback;
    const hh = hhmm.slice(0,2);
    return ` — ${hh}h`;
  }

  // ---------- “Moi” : pseudo & solde depuis la topbar PPP ----------
  function fillMeBoxFromTopbar() {
    const nameEl  = document.querySelector('.user-menu .user-trigger strong');
    const soldeEl = document.querySelector('.solde-box .solde-value');
    const meName  = $('#me-pseudo');
    const mePts   = $('#me-points');

    if (meName && nameEl) meName.textContent = nameEl.textContent.trim();
    if (mePts && soldeEl) mePts.textContent  = soldeEl.textContent.trim() + ' ⛃';
    // Ajout esthétique : "en ligne" (vert via .gp) juste sous le solde
    if (mePts) {
      const container = mePts.closest('.solde-box') || mePts.parentElement || document;
      if (!container.querySelector('.me-online')) {
        mePts.insertAdjacentHTML('afterend', '<div class="me-online gp">Online</div>');
      }
    }
  }

  // ======================================================================
  // Rafraîchit le solde affiché dans la topbar (et #me-points)
  // ======================================================================
  async function refreshTopbarSolde(){
    try{
      const res = await fetch('/api/users/me', { credentials: 'same-origin' });
      if(!res.ok) return;
      const me = await res.json();

      // petit formateur local (indépendant de PPP)
      const fmt = (x) => {
        const v = Math.round((Number(x)||0)*10)/10;
        const s = v.toFixed(1).replace('.', ',');
        return s.replace(/,0$/, ''); // 2,0 -> 2
      };

      const top = document.querySelector('.solde-box .solde-value');
      if (top && me && me.points != null) {
        top.textContent = fmt(me.points);
      }
      const mePts = document.querySelector('#me-points');
      if (mePts && me && me.points != null) {
        mePts.textContent = `${fmt(me.points)} ⛃`;
      }
    }catch(e){
      console.warn('refreshTopbarSolde', e);
    }
  }

  // ---------- Roster (joueurs connectés) ----------
  function isOnlineFrom(u){
    if (typeof u.is_online === 'boolean') return u.is_online;
    if (u.last_seen){
      const t = Date.parse(u.last_seen);
      if (!Number.isNaN(t)) return (Date.now() - t) <= 300000; // 5 min
    }
    return false;
  }

  // 1) on REND la liste (avec avatars) UNE SEULE FOIS
  async function loadRosterOnce(){
    try{
      const res = await fetch('/api/users/roster', {credentials:'same-origin'});
      if (!res.ok) return;
      const roster = await res.json();

      const wrap = $('#roster');
      if (!wrap) return;
      wrap.innerHTML = '';

      // ---- filtrer l'utilisateur courant ----
      const ME = String(window.TRADE_CFG?.USER_ID);
      const others = (roster || []).filter(u => String(u.id) !== ME);

      // ---- rendre les autres joueurs ----
      others.forEach(u => {
        const online = isOnlineFrom(u);
        const card = document.createElement('div');
        card.className = 'user-card' + (online ? ' online' : ' offline');
        card.dataset.uid = u.id; // <- pour le rafraîchissement d’état

        card.innerHTML = `
          <div class="avatar-box">
            <img class="avatar-mini" alt="${u.username}"
                 src="/u/${u.id}/avatar.png"
                 onerror="this.onerror=null;this.src='/static/cabine/assets/avatar.png'">
          </div>
          <div class="col">
            <div class="name">${u.username}</div>
            <div class="presence ${online ? 'on' : 'off'}">
              ${online ? 'En ligne' : 'Hors ligne'}
            </div>
          </div>
        `;
        card.addEventListener('click', ()=> openChat(u));
        wrap.append(card);
      });
    }catch(e){
      console.warn('[trade] roster error', e);
    }
  }

  async function markThreadRead(userId){
    if (!userId && userId !== 0) return;
    try {
      await fetch(`/api/chat/mark-read?user=${encodeURIComponent(userId)}`, {
        method:'POST', credentials:'include'
      });
      // Nettoyage UI défensif (si appelé ailleurs)
      const card = document.querySelector(`.user-card[data-uid="${userId}"]`);
      if (card) {
        card.classList.remove('has-unread');
        const badge = card.querySelector('.avatar-mini .unread-badge, .unread-badge');
        if (badge) badge.remove();
      }
    } catch(e) {}
  }

  async function pollUnread(){
    try{
      const res = await fetch('/api/chat/unread', { credentials:'same-origin' });
      if (!res.ok) return;
      const arr = await res.json();

      // Construire l’ensemble des expéditeurs qui ont du non-lu
      const unreadFrom = new Set(
        (arr || [])
          .filter(x => Number(x.count || 0) > 0)
          .map(x => Number(x.from || x.from_user_id || x.user || x.id))
      );

      unreadFrom.forEach(uid => {
        if (!document.querySelector(`.user-card[data-uid="${uid}"]`)) {
          const wrap = document.querySelector('#roster');
          if (!wrap) return;
          const card = document.createElement('div');
          card.className = 'user-card offline';
          card.dataset.uid = uid;
          card.innerHTML = `
            <div class="avatar-mini"></div>
            <div class="col">
              <div class="name">Joueur #${uid}</div>
              <div class="presence"></div>
            </div>
          `;
          card.addEventListener('click', ()=> openChat({ id: uid, username: `Joueur #${uid}` }));
          wrap.append(card);
        }
      });

      // Synchroniser toutes les cartes (classe + badge 💬)
      document.querySelectorAll('.user-card').forEach(card => {
        const uid = Number(card.getAttribute('data-uid'));   // ← cohérent avec ton markup
        const hasUnread = unreadFrom.has(uid);

        // Classe visuelle (halo violet déjà géré par ton CSS)
        card.classList.toggle('has-unread', hasUnread);

        // Badge 💬 en haut à droite de l’avatar
        const avatar = card.querySelector('.avatar-box') || card;
        if (getComputedStyle(avatar).position === 'static') {
          avatar.style.position = 'relative';
        }
        let badge = avatar.querySelector('.unread-badge');

        if (hasUnread) {
          if (!badge) {
            badge = document.createElement('span');
            badge.className = 'unread-badge';
            badge.textContent = '💬';
            badge.style.position = 'absolute';
            badge.style.right = '2px';
            badge.style.top = '2px';
            badge.style.fontSize = '14px';
            badge.style.lineHeight = '1';
            badge.style.userSelect = 'none';
	    badge.style.zIndex = '1';
            badge.style.filter = 'drop-shadow(0 0 4px rgba(0,0,0,.35))';
            avatar.appendChild(badge);
          }
        } else if (badge) {
          badge.remove();
        }
      });
    } catch(e) {
      // optionnel: console.warn('pollUnread failed', e);
    }
  }

  // 2) on NE rafraîchit que l’état (online/offline), pas les images
  async function refreshPresenceOnly(){
    try{
      const res = await fetch('/api/users/roster', {credentials:'same-origin'});
      if (!res.ok) return;
      const roster = await res.json();

      roster.forEach(u=>{
        const card = document.querySelector(`.user-card[data-uid="${u.id}"]`);
        if (!card) return; // si nouvel utilisateur, on l’ignore jusqu’au prochain vrai refresh manuel
        const online = isOnlineFrom(u);
        card.classList.toggle('online',  online);
        card.classList.toggle('offline', !online);
        // Mettre à jour le libellé "en ligne"
        const label = card.querySelector('.presence');
        if (label) {
          label.classList.toggle('on', online);
          label.classList.toggle('on', online);
          label.classList.toggle('off', !online);
          label.textContent = online ? 'En ligne' : 'Hors ligne';
        }
      });
    }catch(e){}
  }

  async function refreshUnreadBadges(){
    try{
      const res = await fetch('/api/chat/unread-summary', {credentials:'same-origin'});
      if (!res.ok) return;
      const arr = await res.json();

      // Map des non-lus par expéditeur
      const hasUnreadFrom = new Set(arr.map(x => String(x.from_user_id)));
  
      // Nettoie tout le monde d’abord
      $$('.user-card').forEach(card => card.classList.remove('has-unread'));

      // Ajoute la classe pour ceux qui ont des non-lus
      hasUnreadFrom.forEach(uid => {
        const card = document.querySelector(`.user-card[data-uid="${uid}"]`);
        if (card) card.classList.add('has-unread');
      });
    }catch(e){}
  }

  // ---------- Chat minimal ----------
  function openChat(user){
    const dock = $('#chat-dock');
    let panel = dock.querySelector(`.chat[data-uid="${user.id}"]`);
    if (panel) { panel.querySelector('input')?.focus(); return; }

    panel = document.createElement('section');
    panel.className = 'chat';
    panel.dataset.uid = user.id;
    panel.innerHTML = `
      <header>
        <strong>${user.username}</strong>
        <button class="btn btn-close" type="button" title="Fermer">×</button>
      </header>
      <div class="log"></div>
      <footer>
        <input type="text" placeholder="Écrire un message…">
        <button class="btn" type="button">Envoyer</button>
      </footer>
    `;
    dock.append(panel);

    const log   = panel.querySelector('.log');
    const input = panel.querySelector('input');
    // cible le bouton "Envoyer" du footer (pas le bouton Fermer)
    let btn = panel.querySelector('footer button.btn, footer .btn:not(.btn-close)');
    if (!btn) {
      const allBtns = panel.querySelectorAll('footer button, footer .btn, .btn');
      btn = allBtns[allBtns.length - 1];
    }

    async function refresh(){
      try{
        const msgs = await fetch('/api/chat/messages?user='+encodeURIComponent(user.id), {credentials:'same-origin'}).then(r=>r.json());
        log.innerHTML='';
        msgs.forEach(m=>{
          const div=document.createElement('div');
          div.className='msg ' + (String(m.from)===String(window.TRADE_CFG?.USER_ID)?'me':'other');
          div.textContent=m.body;
          log.append(div);
        });
        log.scrollTop = log.scrollHeight;
      }catch(e){}
    }

    // ---- unified send with optimistic append ----
    async function send(){
      const txt = (input.value || '').trim();
      if (!txt) return;

      // optimistic append
      const div=document.createElement('div');
      div.className='msg me';
      div.textContent=txt;
      log.append(div);
      log.scrollTop = log.scrollHeight;
      input.value = '';
      btn.disabled = true;

      try{
        const resp = await fetch('/api/chat/messages', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          credentials:'same-origin',
          body: JSON.stringify({to:user.id, body:txt})
        });

        let payload = null;
        try { payload = await resp.json(); } catch(_) {}

        if (!resp.ok) {
          const msg = (payload && payload.error) ? payload.error : 'Envoi échoué';
	  alert(msg);
          div.textContent = `(échec) ${txt}`;
          throw new Error(msg);
        }

        // ➕ MAJ instantanée du solde si le back l’a renvoyé
        let pts = null;
        if (payload) {
          if (typeof payload.new_points === 'number') {
            pts = payload.new_points;
          } else if (payload.new_points && typeof payload.new_points.points === 'number') {
            // compat si le back renvoie encore un objet { points: ... }
            pts = payload.new_points.points;
          }
        }
        if (pts != null) {
          const top = document.querySelector('.solde-box .solde-value');
          if (top) top.textContent = `${fmtPts(pts)} pts`;
          const mePts = document.querySelector('#me-points');
          if (mePts) mePts.textContent = `${fmtPts(pts)} ⛃`;
        }

        // reconcile with server state (timestamps/ordre)
        await refresh();
        await markThreadRead(user.id);

        // Filet : si commande 'tome' confirmée côté back mais pas de new_points exploitable
        if (pts == null && String(payload?.gift?.cmd || '').toLowerCase() === 'tome') {
          await refreshTopbarSolde();
        }

        // UI immédiate : enlève le halo + le badge 💬
        {
          const card = document.querySelector(`.user-card[data-uid="${user.id}"]`);
          if (card) {
            card.classList.remove('has-unread');
            const badge = card.querySelector('.avatar-mini .unread-badge, .unread-badge');
            if (badge) badge.remove();
          }
        }
      }catch(e){
        // déjà géré ci-dessus, on remet juste le focus
      }finally{
        btn.disabled = false;
        input.focus();
      }
    }

    const timer = setInterval(async () => {
      await refresh();
      await markThreadRead(user.id);

      // Rafraîchissement périodique du solde (filet de sécurité)
      try {
        const top = document.querySelector('.solde-box .solde-value');
        if (top) await refreshTopbarSolde();
      } catch (e) { console.warn('refresh solde', e); }

      // UI immédiate : enlève le halo + le badge 💬
      {
        const card = document.querySelector(`.user-card[data-uid="${user.id}"]`);
        if (card) {
          card.classList.remove('has-unread');
          const badge = card.querySelector('.avatar-mini .unread-badge, .unread-badge');
          if (badge) badge.remove();
        }
      }
    }, 60000);

    // Premier passage : refresh puis marquer lu + MAJ UI
    refresh().then(async () => {
      await markThreadRead(user.id);
      const card = document.querySelector(`.user-card[data-uid="${user.id}"]`);
      if (card) {
        card.classList.remove('has-unread');
        const badge = card.querySelector('.avatar-mini .unread-badge, .unread-badge');
        if (badge) badge.remove();
      }
    });

    panel.querySelector('.btn-close').addEventListener('click', ()=>{
      clearInterval(timer);
      panel.remove();
    });

    // Click uses the same send()
    btn.addEventListener('click', send);

    // Enter also uses the same send(), Shift+Enter fait une nouvelle ligne
    input.addEventListener('keydown', (e)=>{
      if (e.key === 'Enter' && !e.shiftKey){
        e.preventDefault();
        send();
      }
    });
  }

  // ---------- Listings publics ----------
  async function loadListings(){
    try{
      const res = await fetch('/api/trade/listings', {credentials:'same-origin'});
      if (!res.ok) throw new Error('HTTP '+res.status);
      const rows = await res.json();
      const wrap = $('#listings');
      if (!wrap) return;
      wrap.innerHTML = '';

      rows.forEach(r=>{
        // -- Date + Heure (plusieurs sources possibles) --
        const baseDate = r.date_label || r.deadline_key || '';             // "mer. 12 nov. 2025" ou "2025-11-12"
        const hhmmRaw  = (
            r.target_time                              // "15:00"
         || r.time                                     // "15:00"
         || (r.target_dt && String(r.target_dt).slice(11,16)) // "2025-11-12T15:00:00"
         || (r.payload && (r.payload.target_time || (r.payload.target_dt||'').slice(11,16)))
         || ''
        );
        const hhmm   = (typeof hhmmRaw === 'string' && hhmmRaw.length >= 4) ? hhmmRaw.slice(0,5) : '';
        const hhOnly = hhmm ? (hhmm.split(':')[0] || '') : '';
        const hourTxt= hhOnly ? ` — ${hhOnly}h` : '';  // si pas d’heure → rien

        const dateTxt = baseDate + hourTxt;

        // -- Montant / Cotes / Boosts --
        const stakes  = `${fmtPts(r.stake||r.amount||0)} pts`;
        const baseNum = Number(r.base_odds||r.odds||1);
        const base    = baseNum.toFixed(1).replace('.', ',');
        let boosts = '';
        if (r.boosts_count) {
          const addNum = Number(r.boosts_add||0);
          const addTxt = addNum.toFixed(1).replace('.', ',');
          boosts = ` - ${r.boosts_count} ⚡️(x${addTxt})`;
        }
        const sideRaw = String(r.side || r.choice || '').toUpperCase();
        const isRain  = (sideRaw === 'PLUIE' || sideRaw === 'RAIN' || sideRaw === 'RAINY');
        const sideIcon = isRain ? '💧' : '☀️';
        const totalOdds = Number(r.total_odds || (baseNum + Number(r.boosts_add||0)) || baseNum);
        const gpVal = (Number(r.stake||r.amount||0) * totalOdds);
        const gpTxt = fmtPts(gpVal);

        // Si l’API fournit déjà un label HTML, on l’enrichit avec l’heure si manquante
        let lineHtml;
        if (r.label) {
          // On essaie d’insérer l’heure juste après la date si pas déjà présente
          const hasHour = /(\d{1,2})h\b/.test(r.label) || /\d{2}:\d{2}/.test(r.label);
          lineHtml = hasHour ? r.label : r.label.replace(
            (baseDate || '').trim(),
            (baseDate || '').trim() + hourTxt
          );
        } else {
          lineHtml = `${dateTxt} - ${stakes} (x${base})${boosts} - ${sideIcon} <span class="gp">GP: ${gpTxt} pts</span>`;
        }

        const askVal  = (r.ask_price != null) ? Number(r.ask_price) : null;
        const askHtml = askVal != null
          ? `<span class="ask-price">${fmtPts(askVal)} pts</span>`
          : `<span class="ask-price muted">Prix non défini</span>`;

        const row = document.createElement('div');
        row.className = 'listing';
        row.innerHTML = `
          <div class="meta">
            <div class="title">${askHtml}</div>
            <div class="line">${wrapGP(lineHtml)}</div>
          </div>
          <div class="actions"></div>
        `;

        // Actions : Retirer (si c’est mon annonce) / Acheter (sinon)
        const actions = row.querySelector('.actions');
        if (r.is_mine) {
          const btnCancel = document.createElement('button');
          btnCancel.className = 'btn btn-retire';
          btnCancel.type = 'button';
          btnCancel.textContent = 'Retirer';
          btnCancel.addEventListener('click', async ()=>{
            if (!confirm('Retirer cette annonce ?')) return;
            const resp = await fetch(`/api/trade/listings/${r.id}/cancel`, {
              method:'POST',
              credentials:'same-origin'
            });
            if (!resp.ok) { alert('Impossible de retirer.'); return; }
            await loadListings();
          });
          actions.append(btnCancel);
        } else {
          const btnBuy = document.createElement('button');
          btnBuy.className = 'btn btn-buy';
          btnBuy.type = 'button';
          btnBuy.textContent = 'Acheter';
          btnBuy.addEventListener('click', async ()=>{
            if (r.ask_price != null) {
              const ok = confirm(`Acheter cette mise pour ${fmtPts(r.ask_price)} pts ?`);
              if (!ok) return;
            }
            const resp = await fetch(`/api/trade/listings/${r.id}/buy`, {
              method:'POST',
              credentials:'same-origin'
            });
            if (!resp.ok) {
              let msg = 'Achat impossible.';
              try { const j = await resp.json(); if (j && j.error) msg += '\n' + j.error; } catch {}
              alert(msg);
              return;
            }
            alert('Achat réussi !');
            await loadListings();
          });
          actions.append(btnBuy);
        }

        wrap.append(row);
        colorizeGPIn(row);
      });
    }catch(e){
      console.error('[trade] listings error', e);
    }
  }

  // ---------- Sélection → étape “fixer le prix” ----------
  function showSelectedForSale(item){
    const box = $('#selectedListing');
    if (!box) return;

    const stake = Number(item.amount ?? item.stake ?? 0); // mise d’origine (plancher)
    const labelHtml = wrapGP(item.label || 'Mise sélectionnée');
    box.hidden = false;

    // On pré-remplit la valeur avec la mise, et on impose min=stake
    box.innerHTML = `
      <div class="selected-line" id="selectedListingLabel">${labelHtml}</div>
      <div class="selected-price">
        <label for="sellPriceInput">
          Prix de vente (points)
          <small style="opacity:.8;">— minimum&nbsp;: ${fmtPts(stake)} pts</small>
        </label>
        <input id="sellPriceInput"
               type="number"
               step="0.1"
               min="${stake}"
               inputmode="decimal"
               placeholder="1,0"
               value="${(isFinite(stake) ? stake : 0).toFixed(1)}">
        <button id="sellConfirmBtn" class="btn primary" type="button">OK</button>
      </div>
    `;
    colorizeGPIn(box);

    const btnOk = $('#sellConfirmBtn');
    btnOk.addEventListener('click', async ()=>{
      const priceRaw = $('#sellPriceInput')?.value ?? '';
      const ask = Number(String(priceRaw).replace(',', '.'));

      if (!isFinite(ask) || ask <= 0){
        alert("Indique un prix de vente valide (ex: 3.5)");
        return;
      }
      if (ask + 1e-9 < stake){
        alert(`Le prix doit être au moins ${fmtPts(stake)} pts.`);
        return;
      }

      const payload = {
        kind: item.kind || 'PPP',
        bet_id: item.id,
        city: item.city,
        date_label: item.date_label,
        deadline_key: item.deadline_key,
        choice: item.choice,
        // on envoie les deux pour compatibilité backend
        stake: stake,
        amount: item.amount ?? item.stake,
        odds: item.odds,
        boosts_count: item.boosts_count,
        boosts_add: item.boosts_add,
        total_odds: item.total_odds,
        potential_gain: item.potential_gain,
        ask_price: ask,
        label: `${item.label} — Prix: ${fmtPts(ask)} pts`
      };

      try{
        const resp = await fetch('/api/trade/listings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(payload)
        });

        if (!resp.ok) {
          let msg = 'Impossible de créer l’annonce.';
          try {
            const j = await resp.json();
            if (j && j.error) {
              if (j.error === 'price_too_low') {
                const minTxt = j.min_price != null ? fmtPts(j.min_price) : fmtPts(stake);
                msg = `Le prix est trop bas.\nMinimum autorisé : ${minTxt} pts.`;
              } else {
                msg += '\n' + j.error;
              }
            }
          } catch {} // no-op
          alert(msg);
          return;
        }

        await resp.json();
        box.hidden = true;
        box.innerHTML = '';
        await loadListings();
      }catch(e){
        alert('Impossible de créer l’annonce.');
      }
    });
  }    

  // ---------- Menu “Mettre en vente” ----------
  async function openSellMenu() {
    const panel = $('#sellMenu');
    const btn   = $('#sellBtn');
    if (!panel || !btn) return;

    try {
      const res = await fetch('/api/trade/my-bets', { credentials: 'same-origin' });
      const items = res.ok ? await res.json() : [];
      const ul = $('#myBetsList');
      ul.innerHTML = '';

      if (!items.length) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'Aucune mise disponible.';
        ul.append(li);
      } else {
        items.forEach(it => {
          const li = document.createElement('li');
          li.className = 'sell-item';
          li.dataset.id = it.id;
          li.dataset.kind = it.kind || 'PPP';
          li.title = it.label || '';

          // --- 🔥 Construction plus claire du label ---
          const city = it.city || '—';
          const dateTxt = it.date_label || '';
          const timeTxt = it.time_label ? ` — ${it.time_label}` : '';
          const choiceIcon = (String(it.choice || '').toUpperCase() === 'PLUIE') ? '💧' : '☀️';
          const line = `${city} — ${dateTxt}${timeTxt} - ${fmtPts(it.amount)} pts (x${it.odds.toFixed(1)}) - ${choiceIcon}`;

          const boostsTxt = (it.boosts_count > 0)
            ? ` - ${it.boosts_count} ⚡️(x${it.boosts_add.toFixed(1)})`
            : '';
          const gp = fmtPts(it.potential_gain);
          const htmlLine = `${wrapGP(line + boostsTxt)} - <span class="gp">GP: ${gp} pts</span>`;

          li.innerHTML = `<div class="sell-line">${htmlLine}</div>`;

          // Action : sélection pour fixer le prix
          li.addEventListener('click', () => {
            showSelectedForSale(it);
            panel.hidden = true;
          });

          ul.append(li);
        });
      }
    } catch (e) {
      console.warn('[trade] my-bets error', e);
    }

    colorizeGPIn(panel);
    panel.hidden = false;
    onOutsideClick(panel, btn);
  }

  function bindSellMenu(){
    const btn   = $('#sellBtn');
    const panel = $('#sellMenu');
    const close = $('#closeSellMenu');

    if (!btn || !panel) return;

    btn.addEventListener('click', (e)=>{
      e.preventDefault();
      if (!panel.hidden) { panel.hidden = true; return; }
      openSellMenu();
    });

    if (close) {
      close.addEventListener('click', ()=> panel.hidden = true);
    }
  }

  // ---------- Fallback “Créer une annonce” (prompts) ----------
  function bindFallbackCreate(){
    const btn = $('#btn-new-listing');
    if (!btn) return;
    btn.addEventListener('click', async ()=>{
      const city = prompt('Ville ? (ex: Paris)')||'Paris';
      const hours= Number(prompt("Échéance dans combien d'heures ? (ex: 26)"))||24;
      const sideInput = (prompt('Côté ? (PLUIE/PAS_PLUIE)')||'PLUIE').toUpperCase();
      const choice = (sideInput === 'PAS_PLUIE') ? 'PAS_PLUIE' : 'PLUIE';
      const amount = Number(prompt('Mise (points) ?'))||1;
      const odds   = Number(prompt('Cote initiale (ex 1.4) ?'))||1.0;
      const boosts_count = Number(prompt("Nombre d'éclairs ?"))||0;
      const boosts_add   = Number(prompt("Total boosts ajoutés à la cote (ex 10) ?"))||0;
      const total_odds   = odds + boosts_add;
      const potential_gain = Number((amount * total_odds).toFixed(2));
      const ask_price = Number(prompt("Prix de vente demandé ?")||0) || null;
      const expires_at = new Date(Date.now() + hours*3600*1000).toISOString();

      await fetch('/api/trade/listings', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        credentials:'same-origin',
        body: JSON.stringify({
          kind:'PPP', city, choice,
          stake: amount, base_odds: odds,
          boosts_count, boosts_add, total_odds,
          potential_gain, ask_price, expires_at
        })
      });
      await loadListings();
    });
  }

  // ---------- Presence ----------
  async function heartbeat(){
    try{
      let r = await fetch('/api/users/ping', {method:'POST', credentials:'same-origin'});
      if (!r.ok) throw 0;
    }catch(_){
      try{ await fetch('/api/users/heartbeat', {method:'POST', credentials:'same-origin'}); }catch(__){}
    }
  }

  function startPresenceLoops(){
    heartbeat();
    setInterval(heartbeat, 30000);            // ping serveur
    setInterval(refreshPresenceOnly, 15000);  // toggle online/offline sans toucher aux images
    refreshUnreadBadges();
    setInterval(refreshUnreadBadges, 5000);  // badges non-lus
  }

  function bindMeAvatarLink(){
    const meAvatar = document.querySelector('.me-card .avatar-mini');
    if (!meAvatar) return;
    meAvatar.style.cursor = 'pointer';
    meAvatar.setAttribute('role', 'link');
    meAvatar.setAttribute('aria-label', 'Ouvrir la Cabine');
    meAvatar.addEventListener('click', ()=> {
      // adapte l’URL si ta route est différente
      window.location.assign('/cabine');
    });
  }

  // ---------- boot ----------
  document.addEventListener('DOMContentLoaded', () => {
    fillMeBoxFromTopbar();
    bindSellMenu();
    bindFallbackCreate();
    bindMeAvatarLink();
    loadRosterOnce();  
    startPresenceLoops(); 
    setInterval(pollUnread, 5000);    
    pollUnread();
    loadListings();
  });    
})();