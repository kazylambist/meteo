const canvas = document.getElementById("pad");
const ctx = canvas.getContext("2d", { willReadFrequently: true });

const state = { drawing:false, lastX:0, lastY:0, history:[], redoStack:[] };

// Fond du canvas (noir) et crayon blanc par défaut
const CANVAS_BG = "#000000";
const current = { color:"#ffffff", size:6, erasing:false }; // crayon blanc

// --- DPR / mise à l’échelle ---
function setupDPR() {
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  const targetW = Math.round(rect.width * dpr);
  const targetH = Math.round((rect.width * (canvas.height / canvas.width)) * dpr);

  // Sauvegarde du contenu avant redimensionnement
  const tmp = document.createElement("canvas");
  tmp.width = canvas.width; 
  tmp.height = canvas.height;
  tmp.getContext("2d").drawImage(canvas, 0, 0);

  canvas.width = targetW; 
  canvas.height = targetH;

  fillPaperBackground();

  ctx.save();
  ctx.globalCompositeOperation = "source-over";
  ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, 0, 0, canvas.width, canvas.height);
  ctx.restore();

  ctx.lineCap = "round"; 
  ctx.lineJoin = "round";
  ctx.strokeStyle = current.color;
  ctx.lineWidth = current.size * dpr;
}
window.addEventListener("resize", setupDPR);

// Utilitaire : met à jour toutes les zones "solde"
function setBalance(newBalance) {
  if (newBalance === undefined || newBalance === null || isNaN(newBalance)) return;

  const n = Math.max(0, Math.round(Number(newBalance)));
  const text = n.toLocaleString('fr-FR');

  // 1) Tous les sélecteurs déjà gérés
  document.querySelectorAll(
    '[data-role="balance"], [data-balance], #balance, .js-balance, .balance-value'
  ).forEach(el => {
    if (el.tagName === 'INPUT') el.value = text;
    else el.textContent = text;
  });

  // 2) 🔧 Ajout indispensable pour la topbar PPP
  document.querySelectorAll('.solde-box .solde-value, .solde-value').forEach(el => {
    el.textContent = text;
  });

  // 3) (optionnel) Notifier le reste de l’app
  document.dispatchEvent(new CustomEvent('balance:update', { detail: { balance: n } }));
}

// Met à jour l’affichage des boosts (compatible avec anciens sélecteurs "bolts")
function setBoosts(val) {
  if (val === undefined || val === null) return;
  const text = String(val);
  document.querySelectorAll(
    '[data-role="boosts"], #boosts, .js-boosts, ' +   // nouveaux sélecteurs
    '[data-role="bolts"], #bolts, .js-bolts'          // rétro-compat
  ).forEach(el => { el.textContent = text; });
}

// --- Fond noir (évite la transparence) ---
function fillPaperBackground() {
  ctx.save();
  ctx.globalCompositeOperation = "source-over";
  ctx.fillStyle = CANVAS_BG;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.restore();
}

// --- Init ---
function init() {
  canvas.style.background = "#000";
  fillPaperBackground();

  setupDPR();
  ctx.strokeStyle = current.color;
  ctx.lineWidth   = current.size * (window.devicePixelRatio || 1);

  pushHistory(); 
  bindTools(); 
  updateBrushPreview(); 
  addShortcuts();

  const picker = document.getElementById("colorPicker");
  if (picker) picker.value = "#ffffff";
}
document.fonts ? document.fonts.ready.then(init) : init();

// --- Dessin ---
function pointerDown(x,y){ 
  state.drawing=true; 
  [state.lastX,state.lastY]=[x,y]; 
  ctx.beginPath(); 
  ctx.moveTo(x,y); 
}
function pointerMove(x,y){
  if(!state.drawing) return;
  ctx.globalCompositeOperation = "source-over";
  ctx.strokeStyle = current.erasing ? CANVAS_BG : current.color; // gomme = noir
  ctx.lineWidth = current.size * (window.devicePixelRatio || 1);
  ctx.lineTo(x,y); 
  ctx.stroke(); 
  [state.lastX,state.lastY]=[x,y];
}
function pointerUp(){ 
  if(!state.drawing) return; 
  state.drawing=false; 
  ctx.closePath(); 
  pushHistory(); 
}

function getCanvasXY(evt){
  const rect=canvas.getBoundingClientRect(); 
  const dpr=Math.max(1,window.devicePixelRatio||1);
  let clientX,clientY;
  if(evt.touches&&evt.touches[0]){ 
    clientX=evt.touches[0].clientX; 
    clientY=evt.touches[0].clientY; 
  } else { 
    clientX=evt.clientX; 
    clientY=evt.clientY; 
  }
  const x=(clientX-rect.left)*dpr, y=(clientY-rect.top)*dpr; 
  return {x,y};
}

canvas.addEventListener("mousedown", e=>{ const {x,y}=getCanvasXY(e); pointerDown(x,y); });
canvas.addEventListener("mousemove", e=>{ const {x,y}=getCanvasXY(e); pointerMove(x,y); });
canvas.addEventListener("mouseup", pointerUp);
canvas.addEventListener("mouseleave", pointerUp);
canvas.addEventListener("touchstart", e=>{ e.preventDefault(); const {x,y}=getCanvasXY(e); pointerDown(x,y); }, {passive:false});
canvas.addEventListener("touchmove", e=>{ e.preventDefault(); const {x,y}=getCanvasXY(e); pointerMove(x,y); }, {passive:false});
canvas.addEventListener("touchend", e=>{ e.preventDefault(); pointerUp(); }, {passive:false});

// --- Undo/Redo / Historique ---
function pushHistory(){ 
  try { 
    state.history.push(canvas.toDataURL("image/png")); 
    if(state.history.length>50) state.history.shift(); 
    state.redoStack=[]; 
  } catch(_) {}
}
function undo(){ 
  if(state.history.length<=1) return; 
  const last=state.history.pop(); 
  state.redoStack.push(last); 
  const prev=state.history[state.history.length-1]; 
  restoreFromDataURL(prev); 
}
function redo(){ 
  if(!state.redoStack.length) return; 
  const next=state.redoStack.pop(); 
  state.history.push(next); 
  restoreFromDataURL(next); 
}
function restoreFromDataURL(dataUrl){ 
  const img=new Image(); 
  img.onload=()=>{ 
    ctx.clearRect(0,0,canvas.width,canvas.height); 
    ctx.drawImage(img,0,0,canvas.width,canvas.height); 
  }; 
  img.src=dataUrl; 
}

// --- Outils UI ---
function bindTools(){
  // couleurs prédéfinies
  document.querySelectorAll(".color-swatch").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const color=btn.getAttribute("data-color");
      setColor(color);
      document.querySelectorAll(".color-swatch").forEach(b=>b.classList.remove("selected"));
      btn.classList.add("selected");
      const picker = document.getElementById("colorPicker");
      if (picker) picker.value=color;
    });
  });

  // color picker
  const colorPicker=document.getElementById("colorPicker");
  if (colorPicker) {
    colorPicker.addEventListener("input", e=>{ 
      setColor(e.target.value); 
      markColorPickerSelected(); 
    });
  }

  // taille pinceau
  const brushSize=document.getElementById("brushSize");
  if (brushSize) {
    brushSize.addEventListener("input", e=>{ 
      setSize(parseInt(e.target.value,10)); 
    });
  }

  // gomme
  const eraserBtn = document.getElementById("eraser");
  if (eraserBtn) {
    eraserBtn.addEventListener("click", ()=>{
      current.erasing=!current.erasing; 
      eraserBtn.classList.toggle("active", current.erasing); 
    });
  }

  // actions
  const undoBtn = document.getElementById("undo");
  if (undoBtn) undoBtn.addEventListener("click", undo);
  const redoBtn = document.getElementById("redo");
  if (redoBtn) redoBtn.addEventListener("click", redo);

  const clearBtn = document.getElementById("clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", ()=>{
      if(!confirm("Effacer tout le dessin ?")) return;
      ctx.save();
      ctx.globalCompositeOperation = "source-over";
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.restore();
      pushHistory();
    });
  }

  const dlBtn = document.getElementById("download");
  if (dlBtn) dlBtn.addEventListener("click", downloadImage);

  // --- Bouton commentaire ---
  const commentBtn = document.getElementById("commentBtn");
  if (commentBtn) {
    // on s'assure que #result existe puis on branche simplement
    ensureResultElement(commentBtn);
    commentBtn.addEventListener("click", handleComment);
  }
}

// crée un <p id="result"> à côté du bouton si aucun trouvé
function ensureResultElement(anchorBtn){
  let out = document.getElementById("result");
  if (!out) {
    out = document.createElement("p");
    out.id = "result";
    out.style.marginTop = "8px";
    anchorBtn.insertAdjacentElement('afterend', out);
  }
  return out;
}

function markColorPickerSelected(){ 
  document.querySelectorAll(".color-swatch").forEach(b=>b.classList.remove("selected")); 
}
function setColor(c){ 
  current.color=c; 
  current.erasing=false; 
  updateBrushPreview(); 
}
function setSize(s){ 
  current.size=Math.max(1,Math.min(40,s)); 
  updateBrushPreview(); 
}
function updateBrushPreview(){ 
  const dot=document.getElementById("brushDot"); 
  if (dot) {
    dot.style.width=`${Math.max(6,current.size*1.2)}px`; 
    dot.style.height=dot.style.width; 
  }
}
function getPaperColor(){
  return getComputedStyle(document.documentElement)
    .getPropertyValue("--paper").trim() || "#f3efe6";
}
// --- Raccourcis ---
function addShortcuts(){
  window.addEventListener("keydown", e=>{
    if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==="z"){ e.preventDefault(); undo(); }
    if((e.ctrlKey||e.metaKey) && (e.key.toLowerCase()==="y" || (e.shiftKey && e.key.toLowerCase()==="z"))){ e.preventDefault(); redo(); }
  });
}

async function handleComment(){
  const btn = document.getElementById("commentBtn");
  const result = document.getElementById("result");

  const show = (text) => {
    result.textContent = text;
    result.classList.remove("hidden");
  };

  try {
    btn.disabled = true;
    btn.textContent = "Ça réfléchit…";
    result.classList.add("hidden");
    result.textContent = "";

    const dataUrl = await snapshotWithBackground(canvas, "#000000", 768, 0.72);

    const stakeInput = document.getElementById("betAmount");
    const stake = stakeInput ? Math.floor(Math.max(1, Number(stakeInput.value || 0))) : 0;
    if (!stake || stake < 1) {
      await typeInto(result, "Il faut miser au moins 1 point avant d’invoquer ZEUS ⚡");
      return;
    }

    const res = await fetch("/api/comment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ imageDataUrl: dataUrl, stake })
    });

    if (!res.ok) {
      // Essaye d'extraire un JSON pour récupérer balance/boosts/comment
      let raw = "";
      let j = null;
      try {
        raw = await res.text();
        try { j = JSON.parse(raw); } catch { /* pas du JSON */ }
      } catch {}

      // Si le serveur a renvoyé balance/boosts, on met à jour l'UI même en erreur
      if (j && j.balance != null) setBalance(j.balance);
      if (j && (j.boosts != null || j.bolts != null)) setBoosts(j.boosts ?? j.bolts);

      // ✅ Afficher le commentaire même en 400/500 si on l'a
      const commentErr = j && j.comment ? String(j.comment).trim() : "";
      const verdictErr = j && j.verdict ? String(j.verdict) : "";

      // Construire un "extra" semblable au chemin success si on a des infos
      let extraErr = "";
      const multErr = j && Number.isFinite(j.multiplier) ? Number(j.multiplier) : undefined;
      const payoutErr = j && Number.isFinite(j.payout) ? Number(j.payout) : undefined;
      if (multErr !== undefined && payoutErr !== undefined) {
        if (multErr > 0) {
          extraErr += `\n\n💰 Gain: +${payoutErr.toLocaleString('fr-FR', { maximumFractionDigits: 0 })} pts (mise × ${multErr}).`;
          extraErr += `\n⚡ Bonus: +1 boost.`;
        } else {
          // on n'a pas la mise ici, mais tu l'as dans 'stake'
          extraErr += `\n\n💥 Perte: -${stake.toLocaleString('fr-FR')} pts.`;
        }
      }
      if (j && j.balance != null) {
        extraErr += `\n💼 Nouveau solde: ${Math.round(j.balance).toLocaleString('fr-FR')} pts.`;
      }
      const boostsShow = j && (j.boosts ?? j.bolts);
      if (boostsShow !== undefined && boostsShow !== null) {
        extraErr += `\n⚡ Boosts : ${boostsShow}`;
      }

      // Si on a un commentaire → on l'affiche et on arrête là (on ne masque pas l'info utile)
      if (commentErr) {
        const full = verdictErr ? `${commentErr}${extraErr}` : commentErr + (extraErr || "");
        result.classList.remove("hidden");
        if (prefersReducedMotion()) {
          result.textContent = full;
        } else {
          await typeInto(result, full);
        }
        return;
      }

      // Sinon, message d'erreur lisible
      const serverMsg = (j && (j.message || j.error)) || raw || "";
      const msg = `Oups (${res.status}). ${serverMsg || "Le serveur a refusé la requête."}`;
      if (prefersReducedMotion()) {
        show(msg);
      } else {
        await typeInto(result, msg);
      }
      return;
    }

    const data = await res.json().catch(() => ({}));
    const comment = (data && data.comment ? String(data.comment) : "").trim()
                   || "Par les nuages sacrés, ton art rayonne !";

    result.classList.remove("hidden");

    // MAJ solde & boosts si fournis par l'API
    if (data.balance !== undefined && data.balance !== null) {
      setBalance(data.balance);
    }
    const boostsVal = (data.boosts !== undefined && data.boosts !== null)
      ? data.boosts
      : (data.bolts !== undefined && data.bolts !== null)
        ? data.bolts
        : null;
    if (boostsVal !== null) {
      setBoosts(boostsVal);
    }

    // MAJ solde & boosts (avec fallback si balance manquant)
    if (data.balance != null) {
      setBalance(data.balance);
    } else if (typeof stake === 'number' && data && data.verdict) {
      // Fallback : si l'API n'a pas renvoyé "balance", on ajuste localement
      const delta = (data.verdict === 'Beau dessin.')
        ? (Number(data.payout || 0) - stake)
        : -stake;

      const el = document.querySelector('.solde-box .solde-value, .solde-value');
      if (el) {
        const current = Number(String(el.textContent).replace(/\D+/g, '') || 0);
        setBalance(current + delta);
      }
    }

    // Boosts (compat bolts)
    {
      const boostsVal = (data.boosts !== undefined && data.boosts !== null)
        ? data.boosts
        : (data.bolts !== undefined && data.bolts !== null)
          ? data.bolts
          : null;
      if (boostsVal !== null) setBoosts(boostsVal);
    }

    // --- 🔊 Son selon le verdict ---
    if (data && data.verdict) {
      let audioFile = null;
      if (data.verdict === "Beau dessin.") {
        audioFile = "/static/audio/oui.mp3";
      } else if (data.verdict === "Je déteste.") {
        audioFile = "/static/audio/non.mp3";
      }
      if (audioFile) {
        const audio = new Audio(audioFile);
        audio.play().catch(err => console.warn("Erreur lecture audio:", err));
      }
    }

    // 👉 Construire 'extra' AVANT l'affichage, pour l'avoir aussi en mode "motion réduite"
    let extra = "";
    if (data.multiplier !== undefined && data.payout !== undefined) {
      if (data.multiplier > 0) {
        extra += `\n\n💰 Gain: +${(data.payout).toLocaleString('fr-FR', { maximumFractionDigits: 0 })} pts (mise × ${data.multiplier}).`;
        extra += `\n⚡ Bonus: +1 boost.`;
      } else {
        extra += `\n\n💥 Perte: -${stake.toLocaleString('fr-FR')} pts.`;
      }
    }
    if (data.balance !== undefined && data.balance !== null) {
      extra += `\n💼 Nouveau solde: ${Math.round(data.balance).toLocaleString('fr-FR')} pts.`;
    }
    const boostsValForText = (data.boosts ?? data.bolts);
    if (boostsValForText !== undefined && boostsValForText !== null) {
      extra += `\n⚡ Boosts : ${boostsValForText}`;
    }

    const fullText = comment + (extra || "");

    if (prefersReducedMotion()) {
      result.textContent = fullText;
    } else {
      await typeInto(result, fullText);
    }

  } catch (err) {
    console.error(err);
    result.classList.remove("hidden");
    result.textContent = "Oups, impossible d’obtenir le commentaire. Réessaie dans un instant.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Solliciter ZEUS";
  }
}

// --- Redimensionnement + encodage JPEG ---
function toResizedDataURL(srcCanvas, maxSide=1024, quality=0.85){
  return new Promise((resolve)=>{
    const w=srcCanvas.width, h=srcCanvas.height;
    const scale=Math.min(1, maxSide/Math.max(w,h));
    if(scale===1) return resolve(srcCanvas.toDataURL("image/jpeg", quality));
    const off=document.createElement("canvas");
    off.width=Math.round(w*scale); 
    off.height=Math.round(h*scale);
    const octx=off.getContext("2d");
    octx.imageSmoothingEnabled=true; 
    octx.imageSmoothingQuality="high";
    octx.drawImage(srcCanvas,0,0,off.width,off.height);
    resolve(off.toDataURL("image/jpeg", quality));
  });
}

// --- Snapshot non destructif avec fond noir ---
function snapshotWithBackground(srcCanvas, bg = "#000000", maxSide = 1024, quality = 0.85){
  return new Promise((resolve)=>{
    const w = srcCanvas.width, h = srcCanvas.height;
    const scale = Math.min(1, maxSide / Math.max(w, h));
    const outW = Math.round(w * scale), outH = Math.round(h * scale);

    const off = document.createElement("canvas");
    off.width = outW; 
    off.height = outH;
    const octx = off.getContext("2d");

    // 1) peindre d'abord le fond noir
    octx.fillStyle = bg;
    octx.fillRect(0, 0, outW, outH);

    // 2) dessiner le contenu courant par-dessus
    octx.imageSmoothingEnabled = true;
    octx.imageSmoothingQuality = "high";
    octx.drawImage(srcCanvas, 0, 0, w, h, 0, 0, outW, outH);

    resolve(off.toDataURL("image/jpeg", quality));
  });
}

// --- Download ---
async function downloadImage(){
  const url = await snapshotWithBackground(canvas, "#000000", 4096, 0.95);
  const a = document.createElement("a");
  a.href = url;
  a.download = "mon_dessin.jpg"; // JPEG conseillé (fond noir)
  a.click();
}

// --- Effet de dactylo (typewriter)
function prefersReducedMotion(){
  return window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

let __typeTicket = 0; // pour annuler une frappe en cours si on reclique
function sleep(ms){ return new Promise(r=>setTimeout(r, ms)); }

async function typeInto(el, text, speedMs = 18){
  el.textContent = '';
  const cursor = document.createElement('span');
  cursor.className = 'cursor';
  cursor.textContent = '▍';
  el.appendChild(cursor);

  const myTicket = ++__typeTicket;
  for (let i = 0; i < text.length; i++){
    if (myTicket !== __typeTicket) return; // annulé par un nouveau clic
    cursor.insertAdjacentText('beforebegin', text[i]);
    await sleep(speedMs);
  }
  cursor.remove();
}