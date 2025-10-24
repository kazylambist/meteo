const canvas = document.getElementById("pad");
const ctx = canvas.getContext("2d", { willReadFrequently: true });

const state = { drawing: false, lastX: 0, lastY: 0, history: [], redoStack: [] };
const current = { color: "#ffffff", size: 6, erasing: false }; // blanc par défaut pour fond noir

// --- DPR / mise à l’échelle ---
function setupDPR() {
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  const targetW = Math.round(rect.width * dpr);
  const targetH = Math.round((rect.width * (canvas.height / canvas.width)) * dpr);

  const tmp = document.createElement("canvas");
  tmp.width = canvas.width;
  tmp.height = canvas.height;
  tmp.getContext("2d").drawImage(canvas, 0, 0);

  canvas.width = targetW;
  canvas.height = targetH;

  ctx.save();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, 0, 0, canvas.width, canvas.height);
  ctx.restore();

  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.strokeStyle = current.color;
  ctx.lineWidth = current.size * dpr;
}
window.addEventListener("resize", setupDPR);

// --- Fond pour export ou commentaire ---
function fillPaperBackground() {
  ctx.save();
  ctx.globalCompositeOperation = "destination-over";
  ctx.fillStyle = "#000000"; // fond noir
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.restore();
}

// --- Init ---
function init() {
  current.color = document.querySelector(".color-swatch.selected")?.dataset.color || "#ffffff";
  setupDPR();
  fillPaperBackground();
  pushHistory();
  bindTools();
  updateBrushPreview();
  addShortcuts();
}
document.fonts ? document.fonts.ready.then(init) : init();

// --- Dessin ---
function pointerDown(x, y) {
  state.drawing = true;
  [state.lastX, state.lastY] = [x, y];
  ctx.beginPath();
  ctx.moveTo(x, y);
}
function pointerMove(x, y) {
  if (!state.drawing) return;
  ctx.globalCompositeOperation = current.erasing ? "destination-out" : "source-over";
  ctx.strokeStyle = current.erasing ? "#000000" : current.color;
  ctx.lineWidth = current.size * (window.devicePixelRatio || 1);
  ctx.lineTo(x, y);
  ctx.stroke();
  [state.lastX, state.lastY] = [x, y];
}
function pointerUp() {
  if (!state.drawing) return;
  state.drawing = false;
  ctx.closePath();
  pushHistory();
}

// --- Coordonnées ---
function getCanvasXY(evt) {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  let clientX, clientY;
  if (evt.touches && evt.touches[0]) {
    clientX = evt.touches[0].clientX;
    clientY = evt.touches[0].clientY;
  } else {
    clientX = evt.clientX;
    clientY = evt.clientY;
  }
  return { x: (clientX - rect.left) * dpr, y: (clientY - rect.top) * dpr };
}

// --- Events ---
canvas.addEventListener("mousedown", e => { const { x, y } = getCanvasXY(e); pointerDown(x, y); });
canvas.addEventListener("mousemove", e => { const { x, y } = getCanvasXY(e); pointerMove(x, y); });
canvas.addEventListener("mouseup", pointerUp);
canvas.addEventListener("mouseleave", pointerUp);
canvas.addEventListener("touchstart", e => { e.preventDefault(); const { x, y } = getCanvasXY(e); pointerDown(x, y); }, { passive: false });
canvas.addEventListener("touchmove", e => { e.preventDefault(); const { x, y } = getCanvasXY(e); pointerMove(x, y); }, { passive: false });
canvas.addEventListener("touchend", e => { e.preventDefault(); pointerUp(); }, { passive: false });

// --- Undo/Redo / Historique ---
function pushHistory() {
  try {
    state.history.push(canvas.toDataURL("image/png"));
    if (state.history.length > 50) state.history.shift();
    state.redoStack = [];
  } catch (_) { }
}
function undo() {
  if (state.history.length <= 1) return;
  const last = state.history.pop();
  state.redoStack.push(last);
  const prev = state.history[state.history.length - 1];
  restoreFromDataURL(prev);
}
function redo() {
  if (!state.redoStack.length) return;
  const next = state.redoStack.pop();
  state.history.push(next);
  restoreFromDataURL(next);
}
function restoreFromDataURL(dataUrl) {
  const img = new Image();
  img.onload = () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  };
  img.src = dataUrl;
}

// --- Outils UI ---
function bindTools() {
  document.querySelectorAll(".color-swatch").forEach(btn => {
    btn.addEventListener("click", () => {
      const color = btn.dataset.color;
      setColor(color);
      document.querySelectorAll(".color-swatch").forEach(b => b.classList.remove("selected"));
      btn.classList.add("selected");
      const picker = document.getElementById("colorPicker");
      if (picker) picker.value = color;
    });
  });

  const colorPicker = document.getElementById("colorPicker");
  if (colorPicker) colorPicker.addEventListener("input", e => { setColor(e.target.value); markColorPickerSelected(); });

  const brushSize = document.getElementById("brushSize");
  if (brushSize) brushSize.addEventListener("input", e => setSize(parseInt(e.target.value, 10)));

  const eraserBtn = document.getElementById("eraser");
  if (eraserBtn) {
    eraserBtn.addEventListener("click", () => {
      current.erasing = !current.erasing;
      eraserBtn.classList.toggle("active", current.erasing);
    });
  }

  const undoBtn = document.getElementById("undo");
  if (undoBtn) undoBtn.addEventListener("click", undo);
  const redoBtn = document.getElementById("redo");
  if (redoBtn) redoBtn.addEventListener("click", redo);

  const clearBtn = document.getElementById("clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (!confirm("Effacer tout le dessin ?")) return;
      ctx.save();
      ctx.globalCompositeOperation = "source-over";
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      fillPaperBackground();
      ctx.restore();
      pushHistory();
    });
  }

  const dlBtn = document.getElementById("download");
  if (dlBtn) dlBtn.addEventListener("click", downloadImage);

  const commentBtn = document.getElementById("commentBtn");
  if (commentBtn) {
    const resultEl = ensureResultElement(commentBtn);
    commentBtn.addEventListener("click", () => handleComment(commentBtn, resultEl));
  }
}
function ensureResultElement(anchorBtn) {
  let out = document.getElementById("result");
  if (!out) {
    out = document.createElement("p");
    out.id = "result";
    out.style.marginTop = "8px";
    anchorBtn.insertAdjacentElement('afterend', out);
  }
  return out;
}

// --- Couleur / Taille ---
function markColorPickerSelected() {
  document.querySelectorAll(".color-swatch").forEach(b => b.classList.remove("selected"));
}
function setColor(c) {
  current.color = c;
  current.erasing = false;
  updateBrushPreview();
}
function setSize(s) {
  current.size = Math.max(1, Math.min(40, s));
  updateBrushPreview();
}
function updateBrushPreview() {
  const dot = document.getElementById("brushDot");
  if (dot) {
    dot.style.width = `${Math.max(6, current.size * 1.2)}px`;
    dot.style.height = dot.style.width;
  }
}

// --- Raccourcis clavier ---
function addShortcuts() {
  window.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); }
    if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); redo(); }
  });
}

// --- Commentaires ---
async function handleComment() {
  const btn = document.getElementById("commentBtn");
  const result = document.getElementById("result");

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  async function typeInto(el, text, speedMs = 18) {
    el.textContent = '';
    const cursor = document.createElement('span');
    cursor.className = 'cursor';
    cursor.textContent = '▍';
    el.appendChild(cursor);
    for (let i = 0; i < text.length; i++) {
      cursor.insertAdjacentText('beforebegin', text[i]);
      await sleep(speedMs);
    }
    cursor.remove();
  }

  try {
    btn.disabled = true;
    btn.textContent = "Ça réfléchit…";
    result.classList.add("hidden");
    result.textContent = "";

    fillPaperBackground();
    const dataUrl = await toResizedDataURL(canvas, 256, 256);

    const formData = new FormData();
    formData.append("image", dataURLtoBlob(dataUrl), "drawing.png");

    const response = await fetch("/comment", { method: "POST", body: formData });
    const json = await response.json();
    const commentText = json.comment || "…";

    result.classList.remove("hidden");
    await typeInto(result, commentText);

  } catch (err) {
    console.error(err);
    result.textContent = "Erreur lors du commentaire.";
    result.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Commenter";
  }
}
function dataURLtoBlob(dataurl) {
  const parts = dataurl.split(',');
  const mime = parts[0].match(/:(.*?);/)[1];
  const bstr = atob(parts[1]);
  let n = bstr.length;
  const u8arr = new Uint8Array(n);
  while (n--) u8arr[n] = bstr.charCodeAt(n);
  return new Blob([u8arr], { type: mime });
}
async function toResizedDataURL(canvas, width, height) {
  const tmp = document.createElement("canvas");
  tmp.width = width;
  tmp.height = height;
  const tmpCtx = tmp.getContext("2d");
  tmpCtx.fillStyle = "#000000";
  tmpCtx.fillRect(0, 0, width, height);
  tmpCtx.drawImage(canvas, 0, 0, width, height);
  return tmp.toDataURL("image/png");
}

// --- Télécharger ---
function downloadImage() {
  fillPaperBackground();
  const link = document.createElement("a");
  link.href = canvas.toDataURL("image/png");
  link.download = "dessin.png";
  link.click();
}