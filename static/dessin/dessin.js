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

// --- Fond blanc (évite la transparence) ---
function fillPaperBackground() {
  ctx.save();
  ctx.globalCompositeOperation = "source-over"; // on peint, pas de transparence
  ctx.fillStyle = CANVAS_BG;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.restore();
}

// --- Init ---
function init() {
  // Peindre un fond NOIR réel (pas transparent)
  fillPaperBackground();

  setupDPR();
  // Assure que le pinceau reflète bien le blanc par défaut
  ctx.strokeStyle = current.color;
  ctx.lineWidth   = current.size * (window.devicePixelRatio || 1);

  // marquer l'état initial
  pushHistory(); 
  bindTools(); 
  updateBrushPreview(); 
  addShortcuts();

  // UI : si tu as des pastilles/couleurs, marque le blanc comme sélectionné
  const picker = document.getElementById("colorPicker");
  if (picker) picker.value = "#ffffff";
}
// si fonts API dispo, attend sa dispo ; sinon, init direct
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
    // crée/retourne un élément résultat à côté du bouton si absent
    const resultEl = ensureResultElement(commentBtn);
    commentBtn.addEventListener("click", () => handleComment(commentBtn, resultEl));
  }
}

// crée un <p id="result"> à côté du bouton si aucun trouvé
function ensureResultElement(anchorBtn){
  let out = document.getElementById("result");
  if (!out) {
    out = document.createElement("p");
    out.id = "result";
    out.style.marginTop = "8px";
    // insère juste après le bouton
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

  // Effet machine à écrire (léger)
  function typeInto(el, text, speed = 18){
    return new Promise(resolve=>{
      // reset propre
      el.classList.remove("hidden");
      el.textContent = "";
      const cursor = document.createElement("span");
      cursor.className = "cursor";
      cursor.textContent = "▍";
      el.appendChild(cursor);

      let i = 0;
      (function tick(){
        if (i < text.length){
          cursor.insertAdjacentText("beforebegin", text[i++]);
          setTimeout(tick, speed);
        } else {
          resolve();
        }
      })();
    });
  }

  // Affiche immédiatement un message (sans typing)
  const show = (text) => {
    result.textContent = text;
    result.classList.remove("hidden");
  };

  try{
    btn.disabled = true;
    btn.textContent = "Ça réfléchit…";
    result.classList.add("hidden");
    result.textContent = "";

    const dataUrl = await snapshotWithBackground(canvas, "#000000", 768, 0.72);

    const res = await fetch("/api/comment", {
      method: "POST",
      headers: { "Content-Type":"application/json" },
      body: JSON.stringify({ imageDataUrl: dataUrl })
    });

    if (!res.ok) {
      let serverMsg = "";
      try { serverMsg = await res.text(); } catch {}
      await typeInto(result, `Oups (${res.status}). ${serverMsg || "Le serveur a refusé la requête."}`);
      return;
    }

    const data = await res.json().catch(() => ({}));
    const comment = (data && data.comment ? String(data.comment) : "").trim()
                   || "Par les nuages sacrés, ton art rayonne !";

    result.classList.remove("hidden");

    // --- 🔊 Lecture du son selon le verdict ---
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

    // Accessibilité : si motion réduite, pas d'animation
    if (prefersReducedMotion()){
      result.textContent = comment || "Par les nuages sacrés, ton art rayonne !";
    } else {
      await typeInto(result, comment || "Par les nuages sacrés, ton art rayonne !", 16);
    }

    // Effet de frappe pour le commentaire
  } catch (err){
    console.error(err);
    result.classList.remove("hidden");
    result.textContent = "Oups, impossible d’obtenir le commentaire. Réessaie dans un instant.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Montrer à ZEUS";
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