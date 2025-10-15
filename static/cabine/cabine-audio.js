
// cabine-audio.v3.js — Playlist aléatoire + bouton ⬛/▶︎ + mémoire du mute
(function () {
  const CABINE_PATH_PREFIX = '/cabine';
  const AUDIO_DIR = 'assets/audio/';  // dossier des MP3 relatifs à /cabine/
  const AUDIO_FILES = [
    'cabine_music.mp3',   // déjà présent
    'cabine_music_2.mp3',     // ← ajoute ce nouveau fichier dans static/cabine/assets/audio/
    'cabine_music_3.mp3'
  ];

  const BTN_ID = 'cabineStopBtn';
  const LS_KEY_MUTE = 'cabineMusicMuted';
  const LS_KEY_LAST = 'cabineLastTrack';

  function onCabine() {
    try { return location.pathname.startsWith(CABINE_PATH_PREFIX); }
    catch(_) { return false; }
  }

  function findCabineLink() {
    const byHref = Array.from(document.querySelectorAll('a[href]')).find(a => /\/cabine\/?$/.test(a.getAttribute('href')));
    if (byHref) return byHref;
    const byText = Array.from(document.querySelectorAll('a,button')).find(el => (el.textContent || '').trim().toLowerCase() === 'cabine');
    return byText || null;
  }

  function findTopbarContainer() {
    return document.querySelector('.nav-right')
        || document.querySelector('.topbar')
        || document.querySelector('nav')
        || document.querySelector('header')
        || document.body;
  }

  function ensureStopButton() {
    let btn = document.getElementById(BTN_ID);
    if (btn) return btn;

    const link = findCabineLink();
    btn = document.createElement('button');
    btn.id = BTN_ID;
    btn.type = 'button';
    btn.textContent = '⬛';
    btn.className = (link && link.className) ? link.className : 'navlink';
    btn.title = 'Arrêter la musique';
    btn.style.marginRight = '8px';

    if (link && link.parentNode) {
      link.parentNode.insertBefore(btn, link);
    } else {
      findTopbarContainer().appendChild(btn);
    }
    return btn;
  }

  function pickRandomTrack() {
    let files = AUDIO_FILES.slice();
    const last = localStorage.getItem(LS_KEY_LAST);
    if (files.length > 1 && last && files.includes(last)) {
      files = files.filter(f => f !== last); // éviter la répétition immédiate
    }
    const choice = files[Math.floor(Math.random() * files.length)];
    localStorage.setItem(LS_KEY_LAST, choice);
    return AUDIO_DIR + choice;
  }

  async function tryPlay(audio, stopBtn) {
    try {
      await audio.play();
      stopBtn.textContent = '⬛';
      stopBtn.title = 'Arrêter la musique';
      return true;
    } catch (e) {
      stopBtn.textContent = '▶︎';
      stopBtn.title = 'Activer la musique';
      return false;
    }
  }

  function initWhenReady(fn){
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function init() {
    if (!onCabine()) return;

    const src = pickRandomTrack();
    const audio = new Audio(src);
    audio.loop = true;
    audio.preload = 'auto';

    const stopBtn = ensureStopButton();
    let manuallyStopped = localStorage.getItem(LS_KEY_MUTE) === '1';

    if (!manuallyStopped) {
      tryPlay(audio, stopBtn);
    } else {
      stopBtn.textContent = '▶︎';
      stopBtn.title = 'Relancer la musique';
    }

    stopBtn.addEventListener('click', async () => {
      if (!manuallyStopped) {
        audio.pause();
        audio.currentTime = 0;
        manuallyStopped = true;
        localStorage.setItem(LS_KEY_MUTE, '1');
        stopBtn.textContent = '▶︎';
        stopBtn.title = 'Relancer la musique';
      } else {
        const ok = await tryPlay(audio, stopBtn);
        if (ok) {
          manuallyStopped = false;
          localStorage.removeItem(LS_KEY_MUTE);
        }
      }
    });

    window.addEventListener('beforeunload', () => {
      try { audio.pause(); } catch(_){}
    });
  }

  initWhenReady(() => {
    if (!onCabine()) return;
    let started = false;
    const startOnce = () => { if (!started) { started = true; init(); } };

    if (findCabineLink() || document.querySelector('.topbar, .nav-right, nav, header')) {
      startOnce();
      return;
    }

    const obs = new MutationObserver((_m, observer) => {
      if (findCabineLink() || document.querySelector('.topbar, .nav-right, nav, header')) {
        observer.disconnect();
        startOnce();
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(() => { try { obs.disconnect(); } catch(_){} startOnce(); }, 2000);
  });
})();
