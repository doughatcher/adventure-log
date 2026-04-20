// DnD Stage frontend

// ── Minimal markdown renderer (no external deps) ──
function renderMd(text) {
  if (!text) return '';
  // Remove ## PANEL: header lines
  text = text.replace(/^## PANEL:.*$/mg, '').trim();

  const lines = text.split('\n');
  let html = '';
  let inUl = false;

  for (let line of lines) {
    // Headings
    if (/^### (.+)/.test(line)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h3>${md_inline(line.slice(4))}</h3>`;
    } else if (/^## (.+)/.test(line)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h2>${md_inline(line.slice(3))}</h2>`;
    } else if (/^# (.+)/.test(line)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h1>${md_inline(line.slice(2))}</h1>`;
    }
    // HR
    else if (/^---+$/.test(line.trim())) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += '<hr>';
    }
    // List items
    else if (/^[-*] (.+)/.test(line)) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${md_inline(line.slice(2))}</li>`;
    }
    // Blank
    else if (line.trim() === '') {
      if (inUl) { html += '</ul>'; inUl = false; }
    }
    // Paragraph
    else {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<p>${md_inline(line)}</p>`;
    }
  }
  if (inUl) html += '</ul>';
  return html;
}

function md_inline(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>');
}

// ── Panel map ──
const PANELS = {
  'scene':      { el: () => document.getElementById('panel-scene-body'),   wrap: () => document.getElementById('panel-scene') },
  'story-log':  { el: () => document.getElementById('panel-story-body'),   wrap: () => document.getElementById('panel-story') },
  'party':      { el: () => document.getElementById('panel-party-body'),   wrap: () => document.getElementById('panel-party') },
  'next-steps': { el: () => document.getElementById('panel-next-body'),    wrap: () => document.getElementById('panel-next') },
  'map':        { el: () => document.getElementById('panel-map-body'),     wrap: () => document.getElementById('panel-map') },
};

function setPanel(name, content) {
  const p = PANELS[name];
  if (!p) return;
  const el = p.el();
  const wrap = p.wrap();
  if (el) {
    el.innerHTML = renderMd(content);
    // Flash animation
    wrap.classList.remove('updated');
    void wrap.offsetWidth;
    wrap.classList.add('updated');
  }
}

// ── WebSocket ──
let ws;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'init') {
      for (const [name, content] of Object.entries(msg.panels)) {
        setPanel(name, content);
      }
    } else if (msg.type === 'panels') {
      for (const [name, content] of Object.entries(msg.data)) {
        setPanel(name, content);
      }
    } else if (msg.type === 'transcript') {
      updateTranscriptBar(msg.tail);
    } else if (msg.type === 'panels_updated') {
      // Panels were written by Gemma — file watcher will push them separately
    }
  };

  ws.onclose = () => setTimeout(connectWS, 2000);
}

function updateTranscriptBar(tail) {
  const bar = document.getElementById('transcript-bar');
  bar.textContent = tail;
  bar.scrollTop = bar.scrollHeight;
}

// ── Voice recording ──
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let sessionStart = null;
let timerInterval = null;

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  // Use webm/opus if supported, fallback
  const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';

  mediaRecorder = new MediaRecorder(stream, { mimeType });

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) audioChunks.push(e.data);
  };

  // Send a chunk every 8 seconds
  mediaRecorder.start(8000);

  mediaRecorder.onstop = async () => {
    if (audioChunks.length === 0) return;
    const blob = new Blob(audioChunks, { type: mimeType });
    audioChunks = [];
    await sendAudio(blob, mimeType);
  };

  // Auto-send chunks by restarting recorder every 8s
  window._recInterval = setInterval(() => {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
      mediaRecorder.start(8000);
    }
  }, 8000);

  isRecording = true;
  sessionStart = sessionStart || Date.now();
  updateRecUI(true);
  startTimer();
}

function stopRecording() {
  clearInterval(window._recInterval);
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop();
  }
  isRecording = false;
  updateRecUI(false);
}

function toggleRecording() {
  if (isRecording) stopRecording();
  else startRecording().catch(err => alert('Microphone access denied: ' + err.message));
}

async function sendAudio(blob, mimeType) {
  const form = new FormData();
  form.append('audio', blob, 'audio.webm');
  try {
    const resp = await fetch('/api/voice', { method: 'POST', body: form });
    const data = await resp.json();
    if (data.text) {
      const bar = document.getElementById('transcript-bar');
      bar.textContent += '\n' + data.text;
      bar.scrollTop = bar.scrollHeight;
    }
  } catch (e) {
    console.error('STT error', e);
  }
}

function updateRecUI(recording) {
  const el = document.getElementById('rec-indicator');
  const label = document.getElementById('rec-label');
  if (recording) {
    el.classList.add('recording');
    label.textContent = 'RECORDING';
  } else {
    el.classList.remove('recording');
    label.textContent = 'REC OFF';
  }
}

function startTimer() {
  if (timerInterval) return;
  timerInterval = setInterval(() => {
    if (!sessionStart) return;
    const secs = Math.floor((Date.now() - sessionStart) / 1000);
    const h = String(Math.floor(secs / 3600)).padStart(2, '0');
    const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0');
    const s = String(secs % 60).padStart(2, '0');
    document.getElementById('timer').textContent = `${h}:${m}:${s}`;
  }, 1000);
}

// ── Manual update button ──
async function forceUpdate() {
  const btn = document.getElementById('btn-update');
  btn.textContent = 'Updating…';
  btn.disabled = true;
  try {
    await fetch('/api/update', { method: 'POST' });
    setTimeout(() => { btn.textContent = 'Update Stage'; btn.disabled = false; }, 3000);
  } catch(e) {
    btn.textContent = 'Update Stage'; btn.disabled = false;
  }
}

// ── End session ──
async function endSession() {
  if (!confirm('Archive this session and reset? The log will be saved.')) return;
  stopRecording();
  const resp = await fetch('/api/session/end', { method: 'POST' });
  const data = await resp.json();
  alert('Session archived.');
  sessionStart = null;
  document.getElementById('timer').textContent = '00:00:00';
}

// ── Character modal ──
let editingSlug = null;

function openAddChar() {
  editingSlug = null;
  document.getElementById('modal-title').textContent = 'Add Character';
  document.getElementById('char-name').value = '';
  document.getElementById('char-class').value = '';
  document.getElementById('char-hp-cur').value = '';
  document.getElementById('char-hp-max').value = '';
  document.getElementById('char-ac').value = '';
  document.getElementById('char-notes').value = '';
  document.getElementById('modal-overlay').classList.add('open');
  document.getElementById('char-name').focus();
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
}

async function saveCharacter() {
  const name = document.getElementById('char-name').value.trim();
  if (!name) { alert('Name required'); return; }

  const payload = {
    name,
    char_class: document.getElementById('char-class').value.trim(),
    hp_current: parseInt(document.getElementById('char-hp-cur').value) || 0,
    hp_max: parseInt(document.getElementById('char-hp-max').value) || 0,
    ac: parseInt(document.getElementById('char-ac').value) || 0,
    notes: document.getElementById('char-notes').value.trim(),
  };

  const url = editingSlug ? `/api/characters/${editingSlug}` : '/api/characters';
  const method = editingSlug ? 'PATCH' : 'POST';

  const resp = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (data.ok) {
    closeModal();
    // Party panel will update via file watcher
  }
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  connectWS();

  // Load initial panels via HTTP as fallback
  fetch('/api/panels').then(r => r.json()).then(panels => {
    for (const [name, content] of Object.entries(panels)) {
      setPanel(name, content);
    }
  });

  // Load transcript tail
  fetch('/api/transcript').then(r => r.json()).then(data => {
    if (data.tail) updateTranscriptBar(data.tail);
  });

  document.getElementById('rec-indicator').addEventListener('click', toggleRecording);
  document.getElementById('btn-update').addEventListener('click', forceUpdate);
  document.getElementById('btn-end-session').addEventListener('click', endSession);
  document.getElementById('fab-add-char').addEventListener('click', openAddChar);
  document.getElementById('btn-save-char').addEventListener('click', saveCharacter);
  document.getElementById('btn-cancel-char').addEventListener('click', closeModal);

  // Close modal on overlay click
  document.getElementById('modal-overlay').addEventListener('click', (e) => {
    if (e.target === document.getElementById('modal-overlay')) closeModal();
  });

  // Keyboard shortcut: U = force update, R = toggle recording
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'u' || e.key === 'U') forceUpdate();
    if (e.key === 'r' || e.key === 'R') toggleRecording();
  });
});
