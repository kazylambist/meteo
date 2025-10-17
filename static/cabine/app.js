// === Cabine — stockage par utilisateur & sauvegarde serveur ==================

// Config UI récupérée du backend
let PPP_URL = '/ppp';
let USER_ID = null;
let storageKey = 'cabineSelectionsV1:anon'; // sera remplacée si USER_ID connu

async function fetchUIConfig(){
  try{
    const res = await fetch('/api/config_ui', { credentials: 'same-origin' });
    if (!res.ok) throw new Error('config_ui HTTP ' + res.status);
    const cfg = await res.json();
    if (cfg && cfg.PPP_URL) PPP_URL = cfg.PPP_URL;
    if (cfg && cfg.USER_ID != null){
      USER_ID = String(cfg.USER_ID);
      storageKey = `cabineSelectionsV1:${USER_ID}`; // clé par utilisateur
    }
  }catch(e){
    console.warn('[cabine] fetchUIConfig failed:', e);
  }
}

// Ordres d'empilement et d'affichage
const ORDER = [
  'FOND','PIEDS','TORSE','JAMBES','CEINTURE','ARME','ACCESSOIRE','TRONCHE','LUNETTES','CHAPEAU'
];

const CONTROLS_ORDER = ["TRONCHE", "LUNETTES", "CHAPEAU", "JAMBES", "TORSE", "PIEDS", "CEINTURE", "ARME", "ACCESSOIRE", "FOND"];

const Z_INDEX = {
  'FOND': 10
  'PIEDS': 20,
  'TORSE': 30,
  'JAMBES': 35,
  'CEINTURE': 50,
  'ARME': 55,
  'ACCESSOIRE': 60,
  'TRONCHE': 65,
  'LUNETTES': 70,
  'CHAPEAU': 80
};

async function loadManifest(){
  const res = await fetch('assets/manifest.json', { credentials: 'same-origin' });
  if (!res.ok) throw new Error('manifest HTTP ' + res.status);
  return res.json();
}

function el(tag, attrs={}, ...children){
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k,v]) => {
    if(k === 'class') n.className = v;
    else if(k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2).toLowerCase(), v);
    else if(v !== undefined && v !== null) n.setAttribute(k, v);
  });
  children.forEach(c => n.append(c));
  return n;
}

function setLayer(id, src, z){
  let layer = document.getElementById(id);
  if(!layer){
    layer = el('div', {id, class:'layer', style:`z-index:${z}`});
    const stage = document.getElementById('avatar-stage');
    if (stage) stage.append(layer);
  }
  if (!layer) return;
  layer.innerHTML = '';
  if(src){
    const img = el('img', {src});
    layer.append(img);
  }
}

function populateControls(manifest){
  const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
  const controls = document.getElementById('controls');
  if (!controls) return;
  controls.innerHTML = '';

  // Base avatar
  if(manifest.avatar){
    setLayer('layer-avatar', manifest.avatar, 5);
  }

  CONTROLS_ORDER.forEach(cat => {
    const items = (manifest.categories && manifest.categories[cat]) ? manifest.categories[cat] : [];
    const id = `select-${cat.replace(/\s+/g, '_')}`;
    const select = el('select', {id});
    // option vide (servira à “∅” côté bouton custom si tu l’utilises)
    select.append(el('option', {value:''}, ''));
    items.forEach(path => {
      const name = path.split('/').pop()
        .replace(/\.(png|jpe?g|webp|svg)$/i,'')
        .replace(/[_-]+/g,' ');
      select.append(el('option', {value:path}, name));
    });
    if(saved[cat]) select.value = saved[cat];

    select.addEventListener('change', e => {
      const choice = e.target.value || '';
      if(!choice){ setLayer(`layer-${cat}`, null, Z_INDEX[cat]); }
      else { setLayer(`layer-${cat}`, choice, Z_INDEX[cat]); }
    });

    // init couche si sauvegardée
    if(saved[cat]) setLayer(`layer-${cat}`, saved[cat], Z_INDEX[cat]);

    const control = el('div', {class:'control'},
      el('label', {for:id}, cat),
      select
    );
    controls.append(control);
  });
}

// --- Snapshot du mannequin → PNG → POST vers /api/cabine/snapshot ---
async function sendAvatarSnapshot() {
  const stage = document.getElementById('avatar-stage');
  if (!stage) throw new Error('avatar-stage introuvable');

  // Détermine la taille à partir du stage ou d’une image de base
  const baseImg = stage.querySelector('#layer-avatar img');
  const w = Math.round((baseImg?.naturalWidth || stage.clientWidth || 512));
  const h = Math.round((baseImg?.naturalHeight || stage.clientHeight || 512));

  // Canvas
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');

  // Collecte des images à dessiner, dans le bon ordre (fond → avant)
  // Adapte la liste aux catégories que tu utilises réellement
  const DRAW_ORDER = [
    'avatar',      // base
    'FOND', 'PIEDS','JAMBES','CEINTURE','TORSE',
    'ARME','ACCESSOIRE',
    'TRONCHE','LUNETTES','CHAPEAU'
  ];

  const layers = [];
  for (const key of DRAW_ORDER) {
    const layer = stage.querySelector(`#layer-${key.toLowerCase()}`) ||
                  stage.querySelector(`#layer-${key}`);
    const img = layer?.querySelector('img');
    if (img && img.src) layers.push(img.src);
  }

  // Charge toutes les images et dessine-les
  const loadImage = (src) => new Promise((resolve, reject) => {
    const im = new Image();
    im.crossOrigin = 'anonymous'; // safe local
    im.onload = () => resolve(im);
    im.onerror = reject;
    im.src = src;
  });

  for (const src of layers) {
    try {
      const im = await loadImage(src);
      ctx.drawImage(im, 0, 0, w, h);
    } catch (e) {
      console.warn('[cabine] image introuvable pour snapshot:', src, e);
    }
  }

  // Convertir en Blob et envoyer
  const blob = await new Promise(res => canvas.toBlob(res, 'image/png', 0.92));
  if (!blob) throw new Error('toBlob a renvoyé null');

  const fd = new FormData();
  fd.append('file', blob, 'avatar.png');

  const resp = await fetch('/api/cabine/snapshot', {
    method: 'POST',
    body: fd,
    credentials: 'same-origin'
  });
  if (!resp.ok) {
    console.warn('[cabine] snapshot POST non OK:', resp.status);
  }
}

// -------- SAUVEGARDE FIABLE (POST puis GET de vérification, snapshot, puis redirect) ---
function initActions(){
  const saveBtn = document.getElementById('saveBtn');
  const cancelBtn = document.getElementById('cancelBtn');

  if (saveBtn){
    saveBtn.addEventListener('click', async () => {
      // Construire l’objet des sélections depuis les <select>
      const selections = {};
      document.querySelectorAll('#controls select').forEach(sel => {
        // on privilégie le label du parent .control,
        // sinon on tombe sur data-cat (posée plus haut), puis name/id
        const control = sel.closest('.control');
        const labelEl = control ? control.querySelector('label') : null;
        const label =
          (labelEl && labelEl.textContent.trim()) ||
          (sel.dataset.cat ? sel.dataset.cat : '') ||
          sel.name || sel.id || 'UNKNOWN';

        selections[label] = sel.value || '';
      });

      // 1) Sauvegarde locale (toujours)
      localStorage.setItem(storageKey, JSON.stringify(selections));

      // 2) Sauvegarde serveur si connecté
      if (USER_ID){
        try{
          const postResp = await fetch('/api/cabine', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(selections)
          });

          if (!postResp.ok){
            console.warn('[cabine] POST /api/cabine failed:', postResp.status);
          } else {
            // 2.a) Snapshot PNG best-effort (pour Trade)
            try {
              await sendAvatarSnapshot();
            } catch (e) {
              console.debug('[cabine] snapshot ignoré:', e);
            }

            // 2.b) Vérifier que ça a bien été écrit avant de quitter la page
            try{
              const getResp = await fetch('/api/cabine', { credentials:'same-origin' });
              if (getResp.ok){
                const serverData = await getResp.json();
                if (serverData && typeof serverData === 'object'){
                  localStorage.setItem(storageKey, JSON.stringify(serverData));
                }
              }
            }catch(e){
              console.debug('[cabine] GET-verif après POST ignoré:', e);
            }
          }
        }catch(e){
          console.warn('[cabine] POST /api/cabine error:', e);
        }
      } else {
        console.info('[cabine] utilisateur non connecté → prefs locales uniquement');
      }

      // 3) Redirection (après les opérations ci-dessus)
      window.location.href = PPP_URL;
    });
  }

  if (cancelBtn){
    cancelBtn.addEventListener('click', () => {
      window.location.href = PPP_URL;
    });
  }
}

// ----- (facultatif) menus custom déjà en place chez toi ----------------------
// Si tu as un composant de menus custom “combo/overlay” qui marche, garde-le.
// Sinon, aucun changement ici : on ne touche pas l’UI des sélecteurs.

// --- Démarrage ---------------------------------------------------------------
(async function start(){
  // 1) Config (PPP_URL + USER_ID) pour déterminer la clé localStorage
  await fetchUIConfig().catch(()=>{});

  // 2) Manifest (liste des images/catégories)
  const manifest = await loadManifest();

  // 3) Si connecté : tenter de charger les prefs serveur → seed localStorage
  if (USER_ID){
    try{
      const resp = await fetch('/api/cabine', { credentials:'same-origin' });
      if (resp.ok){
        const serverSaved = await resp.json();
        if (serverSaved && Object.keys(serverSaved).length){
          localStorage.setItem(storageKey, JSON.stringify(serverSaved));
        }
      } else {
        console.info('[cabine] GET /api/cabine non OK (', resp.status, ') — ignore');
      }
    }catch(e){
      console.info('[cabine] GET /api/cabine error — ignore (offline?)', e);
    }
  }

  // 4) Construire l’UI
  populateControls(manifest);
  initActions();
  enhanceHoverPreview();

// ----- Menus custom avec preview au survol (un seul ouvert) -----
function enhanceHoverPreview() {
  let openApi = null; // { close: fn } du menu ouvert

  const controls = document.querySelectorAll('#controls .control');
  controls.forEach(control => {
    const labelEl = control.querySelector('label');
    const sel = control.querySelector('select');
    if (!sel || sel.dataset.enhanced === '1') return;
    sel.dataset.enhanced = '1';

    sel.dataset.cat = (labelEl ? labelEl.textContent.trim() : '');

    // masquer visuellement le <select> (on le garde pour l’accessibilité)
    sel.style.position = 'absolute';
    sel.style.opacity = '0';
    sel.style.pointerEvents = 'none';
    sel.tabIndex = -1;

    const wrapper = document.createElement('div');
    wrapper.className = 'combo';
    sel.parentNode.insertBefore(wrapper, sel);
    wrapper.appendChild(sel);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'combo-btn';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');

    const panel = document.createElement('ul');
    panel.className = 'combo-panel overlay';
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;

    const updateBtnText = () => {
      const opt = sel.selectedOptions[0];
      btn.textContent = (opt && opt.value) ? opt.textContent : '∅';
    };
    updateBtnText();

    const makeItem = (opt) => {
      if (!opt.value) return null; // ignore l’option vide
      const li = document.createElement('li');
      li.className = 'combo-item';
      li.setAttribute('role', 'option');
      li.dataset.value = opt.value;
      li.textContent = opt.textContent || opt.value;

      // SURVOL = prévisualise (temporaire)
      li.addEventListener('mouseenter', () => {
        if (sel.value !== opt.value) {
          sel.value = opt.value;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
      });

      // CLIC = valide et ferme
      li.addEventListener('click', () => {
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        updateBtnText();
        closePanel();
      });
      return li;
    };

    Array.from(sel.options).forEach(opt => {
      const li = makeItem(opt);
      if (li) panel.appendChild(li);
    });

    // Entête : si fermé → ouvre ; si ouvert → reset (∅) et ferme
    btn.addEventListener('click', () => {
      if (!panel.hidden) {
        sel.value = '';
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        updateBtnText(); // → '∅'
        closePanel();
      } else {
        openPanel();
      }
    });

    const positionPanel = () => {
      const r = btn.getBoundingClientRect();
      const spaceBelow = window.innerHeight - r.bottom;
      const openUp = spaceBelow < 220;
      panel.style.minWidth = r.width + 'px';
      panel.style.left = '0';
      panel.style.right = '0';
      panel.style.top = openUp ? '' : 'calc(100% + 6px)';
      panel.style.bottom = openUp ? 'calc(100% + 6px)' : '';
      panel.dataset.dir = openUp ? 'up' : 'down';
    };

    const openPanel = () => {
      if (openApi && openApi.close) openApi.close();
      positionPanel();
      panel.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      wrapper.classList.add('open');
      openApi = { close: () => closePanel() };
    };

    const closePanel = () => {
      if (panel.hidden) return;
      panel.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      wrapper.classList.remove('open');
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      if (openApi && openApi.close === closePanel) openApi = null;
    };

    document.addEventListener('click', (e) => { if (!wrapper.contains(e.target)) closePanel(); });
    window.addEventListener('resize', () => { if (!panel.hidden) positionPanel(); });
    window.addEventListener('scroll', () => { if (!panel.hidden) positionPanel(); }, true);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePanel(); });

    sel.addEventListener('change', updateBtnText);

    wrapper.insertBefore(btn, sel);
    wrapper.appendChild(panel);
  });
}

// Dessine l'avatar courant sur un canvas et renvoie un dataURL PNG
async function buildAvatarPngDataURL() {
  const stage = document.getElementById('avatar-stage');
  if (!stage) return null;

  // Taille du stage (fallback 512x512)
  const r = stage.getBoundingClientRect();
  const W = Math.max(1, Math.round(r.width || 512));
  const H = Math.max(1, Math.round(r.height || 512));

  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');

  // Récupère les couches dans l'ordre croissant de z-index
  const layers = Array.from(stage.querySelectorAll('.layer'))
    .map(l => ({ el: l, z: parseInt(l.style.zIndex || '0', 10) || 0 }))
    .sort((a,b) => a.z - b.z);

  // Dessine chaque <img> de chaque layer en respectant l'ordre
  for (const { el } of layers) {
    const img = el.querySelector('img');
    if (!img || !img.complete || !img.naturalWidth) continue;

    // Mise à l’échelle 1:1 par défaut : si tes images couvrent tout le stage,
    // c’est suffisant. Sinon, adapte ici (drawImage avec dimensions).
    ctx.drawImage(img, 0, 0, W, H);
  }

  return canvas.toDataURL('image/png');
}

// Envoie le PNG au backend pour sauvegarde dans static/avatars/<uid>.png
async function sendAvatarSnapshot() {
  try {
    const dataURL = await buildAvatarPngDataURL();
    if (!dataURL) return false;

    const resp = await fetch('/api/cabine/snapshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ png: dataURL })
    });
    return resp.ok;
  } catch (e) {
    console.warn('[cabine] snapshot upload failed:', e);
    return false;
  }
}

})();