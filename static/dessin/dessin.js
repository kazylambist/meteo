const canvas = document.getElementById("pad");
const ctx = canvas.getContext("2d", { willReadFrequently: true });

function setupDPR() {
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  const targetW = Math.round(rect.width * dpr);
  const targetH = Math.round((rect.width * (canvas.height / canvas.width)) * dpr);

  const tmp = document.createElement("canvas");
  tmp.width = canvas.width; tmp.height = canvas.height;
  tmp.getContext("2d").drawImage(canvas, 0, 0);

  canvas.width = targetW; canvas.height = targetH;

  ctx.save();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, 0, 0, canvas.width, canvas.height);
  ctx.restore();

  ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.strokeStyle = current.color;
  ctx.lineWidth = current.size * dpr;
}
window.addEventListener("resize", setupDPR);

const state = { drawing:false, lastX:0, lastY:0, history:[], redoStack:[] };
const current = { color:"#111827", size:6, erasing:false };

function fillWhiteBackground() {
  ctx.save();
  ctx.globalCompositeOperation = "destination-over";
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.restore();
}

function init() {
  ctx.save(); ctx.fillStyle = "#ffffff"; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.restore();
  setupDPR(); pushHistory(); bindTools(); updateBrushPreview(); addShortcuts();
}
document.fonts ? document.fonts.ready.then(init) : init();

function pointerDown(x,y){ state.drawing=true; [state.lastX,state.lastY]=[x,y]; ctx.beginPath(); ctx.moveTo(x,y); }
function pointerMove(x,y){
  if(!state.drawing) return;
  ctx.globalCompositeOperation = current.erasing ? "destination-out" : "source-over";
  ctx.strokeStyle = current.color;
  ctx.lineWidth = current.size * (window.devicePixelRatio || 1);
  ctx.lineTo(x,y); ctx.stroke(); [state.lastX,state.lastY]=[x,y];
}
function pointerUp(){ if(!state.drawing) return; state.drawing=false; ctx.closePath(); pushHistory(); }

function getCanvasXY(evt){
  const rect=canvas.getBoundingClientRect(); const dpr=Math.max(1,window.devicePixelRatio||1);
  let clientX,clientY;
  if(evt.touches&&evt.touches[0]){ clientX=evt.touches[0].clientX; clientY=evt.touches[0].clientY; }
  else { clientX=evt.clientX; clientY=evt.clientY; }
  const x=(clientX-rect.left)*dpr, y=(clientY-rect.top)*dpr; return {x,y};
}

canvas.addEventListener("mousedown", e=>{ const {x,y}=getCanvasXY(e); pointerDown(x,y); });
canvas.addEventListener("mousemove", e=>{ const {x,y}=getCanvasXY(e); pointerMove(x,y); });
canvas.addEventListener("mouseup", pointerUp);
canvas.addEventListener("mouseleave", pointerUp);
canvas.addEventListener("touchstart", e=>{ e.preventDefault(); const {x,y}=getCanvasXY(e); pointerDown(x,y); }, {passive:false});
canvas.addEventListener("touchmove", e=>{ e.preventDefault(); const {x,y}=getCanvasXY(e); pointerMove(x,y); }, {passive:false});
canvas.addEventListener("touchend", e=>{ e.preventDefault(); pointerUp(); }, {passive:false});

function pushHistory(){ try{ state.history.push(canvas.toDataURL("image/png")); if(state.history.length>50) state.history.shift(); state.redoStack=[]; }catch{} }
function undo(){ if(state.history.length<=1) return; const last=state.history.pop(); state.redoStack.push(last); const prev=state.history[state.history.length-1]; restoreFromDataURL(prev); }
function redo(){ if(!state.redoStack.length) return; const next=state.redoStack.pop(); state.history.push(next); restoreFromDataURL(next); }
function restoreFromDataURL(dataUrl){ const img=new Image(); img.onload=()=>{ ctx.clearRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0,canvas.width,canvas.height); }; img.src=dataUrl; }

function bindTools(){
  document.querySelectorAll(".color-swatch").forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const color=btn.getAttribute("data-color");
      setColor(color);
      document.querySelectorAll(".color-swatch").forEach(b=>b.classList.remove("selected"));
      btn.classList.add("selected");
      document.getElementById("colorPicker").value=color;
    });
  });
  const colorPicker=document.getElementById("colorPicker");
  colorPicker.addEventListener("input", e=>{ setColor(e.target.value); markColorPickerSelected(); });
  const brushSize=document.getElementById("brushSize");
  brushSize.addEventListener("input", e=>{ setSize(parseInt(e.target.value,10)); });
  document.getElementById("eraser").addEventListener("click", ()=>{ current.erasing=!current.erasing; document.getElementById("eraser").classList.toggle("active", current.erasing); });
  document.getElementById("undo").addEventListener("click", undo);
  document.getElementById("redo").addEventListener("click", redo);
  document.getElementById("clear").addEventListener("click", ()=>{
    if(!confirm("Effacer tout le dessin ?")) return;
    ctx.save(); ctx.globalCompositeOperation="source-over"; ctx.fillStyle="#ffffff"; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.restore();
    pushHistory();
  });
  document.getElementById("download").addEventListener("click", downloadImage);
  document.getElementById("commentBtn").addEventListener("click", handleComment);
}
function markColorPickerSelected(){ document.querySelectorAll(".color-swatch").forEach(b=>b.classList.remove("selected")); }
function setColor(c){ current.color=c; current.erasing=false; updateBrushPreview(); }
function setSize(s){ current.size=Math.max(1,Math.min(40,s)); updateBrushPreview(); }
function updateBrushPreview(){ const dot=document.getElementById("brushDot"); dot.style.width=`${Math.max(6,current.size*1.2)}px`; dot.style.height=dot.style.width; }

function addShortcuts(){
  window.addEventListener("keydown", e=>{
    if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==="z"){ e.preventDefault(); undo(); }
    if((e.ctrlKey||e.metaKey) && (e.key.toLowerCase()==="y" || (e.shiftKey && e.key.toLowerCase()==="z"))){ e.preventDefault(); redo(); }
  });
}

async function handleComment(){
  const btn=document.getElementById("commentBtn");
  const result=document.getElementById("result");
  try{
    btn.disabled=true; btn.textContent="Ça réfléchit…"; result.classList.add("hidden");
    fillWhiteBackground();
    const dataUrl=await toResizedDataURL(canvas,1024,0.85);

    const res=await fetch("/api/comment",{
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ imageDataUrl: dataUrl })
    });
    if(!res.ok) throw new Error(\`Erreur serveur: \${res.status}\`);
    const data=await res.json();
    const comment=(data.comment||"").toString().trim();
    result.textContent = comment || "Par les nuages sacrés, ton art rayonne !";
    result.classList.remove("hidden");
  }catch(err){
    console.error(err);
    alert("Oups, impossible d’obtenir le commentaire. Réessaie dans un instant.");
  }finally{
    btn.disabled=false; btn.textContent="Obtenir mon commentaire";
  }
}

function toResizedDataURL(srcCanvas, maxSide=1024, quality=0.85){
  return new Promise((resolve)=>{
    const w=srcCanvas.width, h=srcCanvas.height;
    const scale=Math.min(1, maxSide/Math.max(w,h));
    if(scale===1) return resolve(srcCanvas.toDataURL("image/jpeg", quality));
    const off=document.createElement("canvas");
    off.width=Math.round(w*scale); off.height=Math.round(h*scale);
    const octx=off.getContext("2d");
    octx.imageSmoothingEnabled=true; octx.imageSmoothingQuality="high";
    octx.drawImage(srcCanvas,0,0,off.width,off.height);
    resolve(off.toDataURL("image/jpeg", quality));
  });
}

function downloadImage(){
  fillWhiteBackground();
  const url=canvas.toDataURL("image/png");
  const a=document.createElement("a");
  a.href=url; a.download="mon_dessin.png"; a.click();
}
