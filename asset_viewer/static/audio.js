// Shared audio player for the library and the studio: waveform from cached
// server peaks, click-to-seek, one sound at a time, and A/B groups that keep
// the time code when switching (the habit of the original listen pages).
(() => {
'use strict';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const peaksCache = new Map();
const peaksUrl = file => file.replace('/asset/file/', '/asset/peaks/');
const clock = seconds => {
  if (!Number.isFinite(seconds)) return '0:00';
  const whole = Math.max(0, seconds);
  return `${Math.floor(whole / 60)}:${String(Math.floor(whole % 60)).padStart(2, '0')}`;
};

// Markup for one player. `group` + `label` make it part of an A/B set.
function player(item, {group = '', label = '', compact = false} = {}) {
  return `<div class="wave-player${compact ? ' compact' : ''}" data-wave="${esc(item.file)}"${group ? ` data-ab-group="${esc(group)}"` : ''}>
    <button type="button" class="wave-play" aria-label="播放">${label ? `<b>${esc(label)}</b>` : ''}<i></i></button>
    <div class="wave-track"><canvas></canvas><span class="wave-head"></span></div>
    <span class="wave-time">0:00</span>
    <audio preload="none" src="${esc(item.file)}"></audio>
  </div>`;
}

async function peaks(file) {
  if (!peaksCache.has(file)) {
    peaksCache.set(file, fetch(peaksUrl(file), {cache: 'force-cache'}).then(r => (r.ok ? r.json() : {peaks: []})).catch(() => ({peaks: []})));
  }
  return peaksCache.get(file);
}

function draw(root, data) {
  const canvas = root.querySelector('canvas');
  const box = canvas.getBoundingClientRect();
  if (!box.width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(box.width * ratio);
  canvas.height = Math.round(box.height * ratio);
  const ctx = canvas.getContext('2d');
  const style = getComputedStyle(root);
  const played = root._progress || 0;
  const values = data?.peaks || [];
  const bars = Math.max(1, Math.floor(box.width / 3));
  const mid = canvas.height / 2;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  for (let i = 0; i < bars; i += 1) {
    let amp = 0.04;
    if (values.length) {
      const from = Math.floor((i / bars) * values.length);
      const to = Math.max(from + 1, Math.floor(((i + 1) / bars) * values.length));
      for (let j = from; j < to; j += 1) amp = Math.max(amp, values[j]);
    }
    const h = Math.max(1.5 * ratio, amp * mid * 0.92);
    ctx.fillStyle = i / bars < played ? style.getPropertyValue('--wave-played') : style.getPropertyValue('--wave-idle');
    ctx.fillRect(i * 3 * ratio, mid - h, 2 * ratio, h * 2);
  }
}

function init(root) {
  if (root._ready) return;
  root._ready = true;
  const audio = root.querySelector('audio');
  const time = root.querySelector('.wave-time');
  const render = () => draw(root, root._peaks);
  peaks(root.dataset.wave).then(data => {
    root._peaks = data;
    if (data.duration) time.textContent = clock(data.duration);
    render();
  });
  audio.addEventListener('loadedmetadata', () => { time.textContent = clock(audio.duration); });
  audio.addEventListener('play', () => {
    root.classList.add('playing');
    document.querySelectorAll('.wave-player audio').forEach(other => { if (other !== audio) other.pause(); });
    const tick = () => {
      root._progress = audio.duration ? audio.currentTime / audio.duration : 0;
      root.querySelector('.wave-head').style.left = `${root._progress * 100}%`;
      time.textContent = `${clock(audio.currentTime)} / ${clock(audio.duration)}`;
      render();
      if (!audio.paused) root._raf = requestAnimationFrame(tick);
    };
    tick();
  });
  audio.addEventListener('pause', () => { root.classList.remove('playing'); cancelAnimationFrame(root._raf); });
  audio.addEventListener('ended', () => { root.classList.remove('playing'); });
  new ResizeObserver(render).observe(root.querySelector('.wave-track'));
}

function mount(scope = document) {
  scope.querySelectorAll('.wave-player').forEach(player => visible.observe(player));
}

function seek(root, fraction) {
  const audio = root.querySelector('audio');
  const apply = () => {
    audio.currentTime = fraction * audio.duration;
    root._progress = fraction;
    root.querySelector('.wave-head').style.left = `${fraction * 100}%`;
    draw(root, root._peaks);
  };
  if (audio.readyState >= 1) apply();
  else {
    audio.addEventListener('loadedmetadata', apply, {once: true});
    audio.load();  // preload="none": fetch metadata now
  }
}

// A/B: start the chosen player at the time code of whichever one was playing.
function play(root) {
  const audio = root.querySelector('audio');
  const group = root.dataset.abGroup;
  if (group) {
    const peers = [...document.querySelectorAll(`.wave-player[data-ab-group="${CSS.escape(group)}"]`)].filter(p => p !== root);
    const source = peers.map(p => p.querySelector('audio')).find(a => !a.paused) || peers.map(p => p.querySelector('audio')).find(a => a.currentTime > 0);
    if (source && source.currentTime) {
      const at = source.currentTime;
      const go = () => { audio.currentTime = Math.min(at, audio.duration || at); audio.play(); };
      if (audio.readyState >= 1) go();
      else {
        audio.addEventListener('loadedmetadata', go, {once: true});
        audio.load();
      }
      document.querySelectorAll(`.wave-player[data-ab-group="${CSS.escape(group)}"]`).forEach(p => p.classList.toggle('ab-active', p === root));
      return;
    }
    document.querySelectorAll(`.wave-player[data-ab-group="${CSS.escape(group)}"]`).forEach(p => p.classList.toggle('ab-active', p === root));
  }
  audio.paused ? audio.play() : audio.pause();
}

document.addEventListener('click', event => {
  const button = event.target.closest('.wave-play');
  if (button) {
    event.preventDefault();
    event.stopPropagation();
    const root = button.closest('.wave-player');
    init(root);
    play(root);
    return;
  }
  const track = event.target.closest('.wave-track');
  if (track) {
    event.preventDefault();
    event.stopPropagation();
    const root = track.closest('.wave-player');
    init(root);
    const box = track.getBoundingClientRect();
    seek(root, Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)));
  }
}, true);

// A library topic can hold hundreds of clips: draw a waveform only when it
// scrolls into view, and touch the audio file only when someone presses play.
const visible = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    visible.unobserve(entry.target);
    init(entry.target);
  }
}, {rootMargin: '200px'});
new MutationObserver(records => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeType !== 1) continue;
    if (node.matches?.('.wave-player')) visible.observe(node);
    node.querySelectorAll?.('.wave-player').forEach(player => visible.observe(player));
  }
}).observe(document.documentElement, {childList: true, subtree: true});

function pauseAll() {
  document.querySelectorAll('.wave-player audio').forEach(audio => audio.pause());
}

window.ARAudio = {player, mount, pauseAll};
})();
