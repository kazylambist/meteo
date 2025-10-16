// static/trade/app.js
(() => {
  // ---------- utils ----------
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

  // ---------- “Moi” : pseudo & solde depuis la topbar PPP ----------
  function fillMeBoxFromTopbar() {
    const nameEl  = document.querySelector('.user-menu .user-trigger strong');
    const soldeEl = document.querySelector('.solde-box .solde-value');
    const meName  = $('#me-pseudo');
    const mePts   = $('#me-points');

    if (meName && nameEl) meName.textContent = nameEl.textContent.trim();
    if (mePts && soldeEl) mePts.textContent  = soldeEl.textContent.trim() + ' ⛃';
  }

  // ---------- Roster (joueurs connectés) ----------
  function isOnlineFrom(u){
    if (typeof u.is_online === 'boolean') return u.is_online;
    if (u.last_seen){
      const t = Date.parse(u.last_seen);
      if (!Number.isNaN(t)) return (Date.now() - t) <= 120000; // 2 min
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

      roster.forEach(u => {
        const online = isOnlineFrom(u);
        const card = document.createElement('div');
        card.className = 'user-card' + (online ? ' online' : ' offline');
        card.dataset.uid = u.id; // <- pour le rafraîchissement d’état

        card.innerHTML = `
          <img class="avatar-mini" alt="${u.username}"
               src="/u/${u.id}/avatar.png"
               onerror="this.onerror=null;this.src='/static/cabine/assets/avatar.png'">
          <div class="col">
            <div class="name">${u.username}</div>
            <div class="solde">${(Math.round((Number(u.solde)||0)*100)/100).toString().replace('.', ',')} pts</div>
          </div>
        `;
        card.addEventListener('click', ()=> openChat(u));
        wrap.append(card);
      });
    }catch(e){
      console.warn('[trade] roster error', e);
    }
  }

  async function markThreadRead(otherUserId){
    // marque tous les messages de other->me comme lus côté serveur
    try{
      await fetch('/api/chat/mark-read?user='+encodeURIComponent(otherUserId), {
        method:'POST',
        credentials:'same-origin'
      });
      const card = document.querySelector(`.user-card[data-uid="${otherUserId}"]`);
      if (card) card.classList.remove('has-unread');
    }catch(e){}
  }

  async function pollUnread(){
    // attend un JSON du type: [{from: "USER_ID", count: 3}, ...]
    try{
      const res = await fetch('/api/chat/unread', {credentials:'same-origin'});
      if (!res.ok) return;
      const arr = await res.json();

      // d’abord on enlève les états existants
      document.querySelectorAll('.user-card.has-unread').forEach(el=>el.classList.remove('has-unread'));

      // puis on marque ceux qui ont du non-lu
      arr.forEach(item=>{
        if ((item.count||0) > 0){
          const card = document.querySelector(`.user-card[data-uid="${item.from}"]`);
          if (card) card.classList.add('has-unread');
        }
      });
    }catch(e){}
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

    const log     = panel.querySelector('.log');
    const inputEl = panel.querySelector('input');
    const sendBtn = panel.querySelector('button.btn');

    function appendMsg(text, who){
      const div = document.createElement('div');
      div.className = 'msg ' + (who==='me' ? 'me' : 'other');
      div.textContent = text;
      log.append(div);
      log.scrollTop = log.scrollHeight;
    }

    async function refresh(){
      try{
        const msgs = await fetch('/api/chat/messages?user='+encodeURIComponent(user.id), {credentials:'same-origin'}).then(r=>r.json());
        log.innerHTML='';
        msgs.forEach(m=>{
          appendMsg(m.body, String(m.from)===String(window.TRADE_CFG?.USER_ID)?'me':'other');
        });
      }catch(e){}
    }
    const timer = setInterval(refresh, 4000);
    refresh();

    panel.querySelector('.btn-close').addEventListener('click', ()=>{
      clearInterval(timer);
      panel.remove();
    });

    // --- Envoi message: bouton + Enter ---
    async function sendCurrent(){
      const txt = (inputEl.value || '').trim();
      if (!txt) return;
      try{
        const res = await fetch('/api/chat/messages', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          credentials:'same-origin',
          body: JSON.stringify({to:user.id, body:txt})
        });
        if (!res.ok) throw 0;
        // rendu optimiste immédiat
        appendMsg(txt, 'me');
        inputEl.value = '';
        // on peut relancer un refresh léger pour récupérer l’ID serveur si besoin
        // (non obligatoire pour l’affichage)
        // refresh();
        // marquer comme lu (au cas où des “other” venaient d’arriver)
        try { await markThreadRead(user.id); } catch(_){}
      }catch(e){
        alert("Échec d'envoi.");
      }
    }

    sendBtn.addEventListener('click', sendCurrent);

    // Enter => envoi, sans fermer le panneau
    inputEl.addEventListener('keydown', (e)=>{
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        sendCurrent();
      }
    });

    // Quand on ouvre la fenêtre, on “mark as read”
    markThreadRead(user.id).catch(()=>{});
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
        const dateTxt = r.date_label || r.deadline_key || '';
        const stakes  = `${fmtPts(r.stake||r.amount||0)} pts`;
        const baseNum = Number(r.base_odds||r.odds||1);
        const base    = baseNum.toFixed(1).replace('.', ',');
        let boosts = '';
        if (r.boosts_count) {
          const addNum = Number(r.boosts_add||0);
          const addTxt = addNum.toFixed(1).replace('.', ',');
          boosts = ` - ${r.boosts_count} ⚡️(x${addTxt})`;
        }
        const sideIcon = (String(r.side||r.choice).toUpperCase()==='PLUIE')?'💧':'☀️';
        const totalOdds = Number(r.total_odds || (baseNum + Number(r.boosts_add||0)) || baseNum);
        const gpVal = (Number(r.stake||r.amount||0) * totalOdds);
        const gpTxt = fmtPts(gpVal);

        const lineHtml = r.label
          ? r.label
          : `${dateTxt} - ${stakes} (x${base})${boosts} - ${sideIcon} <span class="gp">GP: ${gpTxt} pts</span>`;

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

    const labelHtml = wrapGP(item.label || 'Mise sélectionnée');
    box.hidden = false;

    box.innerHTML = `
      <div class="selected-line" id="selectedListingLabel">${labelHtml}</div>
      <div class="selected-price">
        <label for="sellPriceInput">Prix de vente (points)</label>
        <input id="sellPriceInput" type="number" step="0.1" min="0" inputmode="decimal" placeholder="1,0">
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

      const payload = {
        kind: item.kind || 'PPP',
        bet_id: item.id,
        city: item.city,
        date_label: item.date_label,
        deadline_key: item.deadline_key,
        choice: item.choice,
        amount: item.amount,
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
          try { const j = await resp.json(); if (j && j.error) msg += '\n' + j.error; } catch {}
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
  async function openSellMenu(){
    const panel = $('#sellMenu');
    const btn   = $('#sellBtn');
    if (!panel || !btn) return;

    try{
      const res = await fetch('/api/trade/my-bets', {credentials:'same-origin'});
      const items = res.ok ? await res.json() : [];
      const ul = $('#myBetsList');
      ul.innerHTML = '';

      if (!items.length) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = 'Aucune mise disponible.';
        ul.append(li);
      } else {
        items.forEach(it=>{
          const li = document.createElement('li');
          li.className = 'sell-item';
          li.dataset.id = it.id;
          li.dataset.kind = it.kind || 'PPP';
          li.title = it.label || '';
          li.innerHTML = `<div class="sell-line">${wrapGP(it.label || '')}</div>`;

          // Ici on n'annonce pas encore : étape “fixer le prix”
          li.addEventListener('click', ()=>{
            showSelectedForSale(it);
            panel.hidden = true;
          });

          ul.append(li);
        });
      }
    }catch(e){
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
    loadRosterOnce();        // rendu initial (avec avatars)
    startPresenceLoops();    // met à jour l’état online/offline
    setInterval(pollUnread, 5000);
    pollUnread();
    loadListings();
  });    
})();