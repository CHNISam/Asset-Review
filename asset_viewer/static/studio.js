// Asset Review studio: production assets, design views, revisions, locks,
// stages and the making-of. The file library (app.js) lives beside it.
(() => {
'use strict';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const PROJECT_KEY = 'asset-review.project';
const S = {csrf: '', meta: null, projects: [], project: '', overview: null, asset: null, route: {name: 'overview'}, viewer: null, showCulled: false};

function readPref(key, fallback = '') {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private window */ }
}
const toast = (message, options) => window.AR?.library?.toast?.(message, options);
const fail = error => { console.error(error); toast(error.message || '出现错误', {error: true}); };

async function api(path) {
  const response = await fetch(path, {cache: 'no-store'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

async function csrf() {
  if (!S.csrf) S.csrf = (await api('/api/session')).csrf;
  return S.csrf;
}

async function post(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Asset-Viewer-CSRF': await csrf()},
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `操作失败（${response.status}）`);
  return data;
}

async function upload(key, target, file) {
  const params = new URLSearchParams({key, ...target});
  const response = await fetch(`/api/production/upload?${params}`, {
    method: 'POST',
    headers: {'Content-Type': file.type || 'application/octet-stream', 'X-Asset-Viewer-CSRF': await csrf(), 'X-Filename': encodeURIComponent(file.name)},
    body: file,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || '上传失败');
  return data;
}

function pickFiles(accept, multiple = true) {
  return new Promise(resolve => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = accept;
    input.multiple = multiple;
    input.onchange = () => resolve([...input.files]);
    input.click();
  });
}

// ------------------------------------------------------------------ formatting

function day(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', {month: 'long', day: 'numeric'});
}
function stamp(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false});
}
function hue(text) {
  let h = 0;
  for (const char of String(text)) h = (h * 31 + char.codePointAt(0)) % 360;
  return h;
}
const STATUS_TEXT = {todo: '未开始', wip: '进行中', ready: '待验收', accepted: '已验收'};
const GATE_ICON = {approved: '✓', review: '!', progress: '◐', none: '', na: ''};
const ORIGIN_TEXT = {discovered: '目录发现', uploaded: '上传', agent: 'Agent 提交', linked: '手动归入'};

// The CSP forbids style attributes in markup; positions are applied from data-*.
function applyStyles(root) {
  root?.querySelectorAll('[data-x]').forEach(el => { el.style.left = `${el.dataset.x * 100}%`; el.style.top = `${el.dataset.y * 100}%`; });
  root?.querySelectorAll('[data-hue]').forEach(el => el.style.setProperty('--h', el.dataset.hue));
  root?.querySelectorAll('[data-cut]').forEach(el => el.style.setProperty('--cut', `${el.dataset.cut}%`));
}

function media(item, size = 'preview', attrs = '') {
  if (!item) return '<div class="media-empty"></div>';
  if (item.media_type === 'video') return `<video src="${esc(item.file)}" poster="${esc(item.thumb)}" preload="metadata" controls playsinline ${attrs}></video>`;
  return `<img src="${esc(item[size] || item.thumb)}" alt="" loading="lazy" decoding="async" ${attrs}>`;
}

// ------------------------------------------------------------------ routing

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'a' && parts[1]) return {name: 'asset', key: parts[1], view: parts[2] === 'v' ? parts[3] : '', stage: parts[2] === 's' ? parts[3] : ''};
  if (parts[0] === 'decide') return {name: 'decide'};
  return {name: 'overview'};
}

function go(hash, {replace = false} = {}) {
  const url = `/${hash}`;
  if (location.pathname + location.hash !== url) history[replace ? 'replaceState' : 'pushState'](null, '', url);
  route().catch(fail);
}

function setMode(mode) {
  document.body.dataset.mode = mode;
  $('#studioView').classList.toggle('hidden', mode !== 'studio');
  $('#libraryView').classList.toggle('hidden', mode !== 'library');
  document.querySelectorAll('[data-nav]').forEach(link => link.classList.remove('active'));
  if (mode === 'library') {
    closeViewer();
    document.title = '素材库 · Asset Review';
  }
}

function showLibrary() {
  if (document.body.dataset.mode === 'library') return;
  setMode('library');
  window.AR.library.syncUrl();
}

async function route() {
  setMode('studio');
  S.route = parseRoute();
  window.scrollTo({top: 0});
  if (S.route.name === 'asset') {
    await loadAsset(S.route.key);
    if (S.route.view) openViewer(S.route.view);
    else closeViewer();
    if (S.route.stage) requestAnimationFrame(() => document.getElementById(`stage-${S.route.stage}`)?.scrollIntoView({block: 'center'}));
  } else {
    closeViewer();
    await loadOverview();
    S.route.name === 'decide' ? renderDecide() : renderOverview();
  }
  renderSidebar();
}

// ------------------------------------------------------------------ data

async function loadProjects() {
  S.projects = (await api('/api/production/projects')).projects;
  const saved = readPref(PROJECT_KEY);
  if (!S.project) S.project = S.projects.some(p => p.name === saved) ? saved : (S.projects[0]?.name || '');
}

async function loadOverview() {
  if (!S.project) { S.overview = null; return; }
  S.overview = await api(`/api/production/overview?project=${encodeURIComponent(S.project)}`);
}

async function loadAsset(key) {
  S.asset = await api(`/api/production/asset?key=${encodeURIComponent(key)}`);
  if (S.asset.asset.project !== S.project) {
    S.project = S.asset.asset.project;
    writePref(PROJECT_KEY, S.project);
    await loadOverview();
  } else if (!S.overview) await loadOverview();
  renderAsset();
}

function applyDetail(detail) {
  S.asset = detail;
  renderAsset();
  if (S.viewer) renderViewer();
  loadOverview().then(renderSidebar).catch(console.error);
}

// ------------------------------------------------------------------ sidebar

function renderSidebar() {
  const project = S.projects.find(p => p.name === S.project);
  const decisions = S.overview?.counts?.decisions || 0;
  $('#switcherName').textContent = S.project || 'Asset Review';
  $('#switcherMeta').textContent = project ? [project.assets ? `${project.assets} 个资产` : '', project.collections ? `${project.collections} 个素材目录` : ''].filter(Boolean).join(' · ') : '';
  const mark = $('#projectMark');
  mark.textContent = (S.project || 'A').trim().charAt(0).toUpperCase();
  mark.style.setProperty('--h', hue(S.project));
  $('#decideCount').textContent = decisions || '';
  $('#decideCount').classList.toggle('hidden', !decisions);
  const studio = document.body.dataset.mode === 'studio';
  document.querySelectorAll('[data-nav]').forEach(link => link.classList.toggle('active', studio && link.dataset.nav === S.route.name));
  const scenes = S.overview?.scenes || [];
  $('#assetNav').innerHTML = scenes.length ? scenes.map(group => `
    ${scenes.length > 1 || group.scene ? `<div class="asset-nav-scene">${esc(group.scene || '未分场景')}</div>` : ''}
    ${group.assets.map(item => {
      const active = studio && S.route.name === 'asset' && S.route.key === item.asset.key;
      const owner = item.decisions.length;
      return `<a class="asset-link${active ? ' active' : ''}" href="/#/a/${esc(item.asset.key)}" data-go="#/a/${esc(item.asset.key)}">
        <span class="asset-link-thumb">${item.cover ? `<img src="${esc(item.cover.thumb)}" alt="" loading="lazy">` : ''}</span>
        <span class="asset-link-name">${esc(item.asset.name)}</span>
        ${owner ? `<span class="asset-link-flag" title="${owner} 项待你决定">${owner}</span>` : `<span class="asset-link-progress">${item.progress.done}/${item.progress.total}</span>`}
      </a>`;
    }).join('')}`).join('') : '<div class="side-empty">还没有资产</div>';
  const libraryProjects = window.AR?.library?.projectNames?.() || [];
  $('#libraryEmpty').classList.toggle('hidden', !S.project || libraryProjects.includes(S.project));
}

function renderProjectMenu() {
  requestAnimationFrame(() => applyStyles($('#projectMenu')));
  $('#projectMenu').innerHTML = `<div class="menu-label">项目</div>${S.projects.map(p => `
    <button type="button" class="menu-item" role="menuitem" data-project-pick="${esc(p.name)}">
      <span class="project-mark small" data-hue="${hue(p.name)}">${esc(p.name.charAt(0).toUpperCase())}</span>
      <span class="menu-text"><strong>${esc(p.name)}</strong><small>${[p.assets ? `${p.assets} 个资产` : '', p.collections ? `${p.collections} 个素材目录` : ''].filter(Boolean).join(' · ') || '空项目'}</small></span>
      ${p.name === S.project ? '<span class="check">✓</span>' : ''}
    </button>`).join('')}<div class="menu-sep"></div><button type="button" class="menu-item" role="menuitem" data-project-new><span class="project-mark small ghost">+</span><span class="menu-text"><strong>新项目</strong><small>从第一个资产开始</small></span></button>`;
}

function toggleProjectMenu(open) {
  const menu = $('#projectMenu');
  const show = open ?? menu.classList.contains('hidden');
  if (show) renderProjectMenu();
  menu.classList.toggle('hidden', !show);
  $('#projectSwitcher').setAttribute('aria-expanded', String(show));
}

async function switchProject(name) {
  toggleProjectMenu(false);
  if (!name || name === S.project) return;
  S.project = name;
  writePref(PROJECT_KEY, name);
  window.AR?.library?.setProject?.(name)?.catch?.(console.error);
  go('#/');
}

// ------------------------------------------------------------------ overview

function stageTrack(stages) {
  return `<div class="stage-track" aria-label="制作阶段">${stages.map(stage => `<span class="track-step ${esc(stage.status)}" title="${esc(stage.label)} · ${esc(STATUS_TEXT[stage.status] || '')}"><i></i><em>${esc(stage.label)}</em></span>`).join('')}</div>`;
}

function assetCard(item) {
  const next = item.next || {};
  return `<a class="work-card" href="/#/a/${esc(item.asset.key)}" data-go="#/a/${esc(item.asset.key)}">
    <figure class="work-cover">${item.cover ? media(item.cover, 'preview') : `<div class="cover-empty"><span>${esc(item.asset.name.charAt(0))}</span></div>`}
      ${item.cover ? `<figcaption>${esc(item.cover.label)}</figcaption>` : ''}</figure>
    <div class="work-body">
      <div class="work-title"><strong>${esc(item.asset.name)}</strong><span>${esc(item.asset.kind_label)}</span></div>
      ${stageTrack(item.stages)}
      <div class="work-next${next.owner ? ' owner' : ''}${next.done ? ' done' : ''}"><i></i>${esc(next.text || '')}</div>
    </div>
  </a>`;
}

function decisionRow(decision, thumb) {
  const target = decision.type === 'select' ? `#/a/${decision.asset}/v/${decision.view}` : decision.type === 'accept' ? `#/a/${decision.asset}/s/${decision.stage}` : `#/a/${decision.asset}`;
  return `<a class="decision-card" href="/${esc(target)}" data-go="${esc(target)}">
    <span class="decision-thumb">${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy">` : ''}</span>
    <span class="decision-copy"><small>${esc(decision.asset_name)}</small><strong>${esc(decision.text)}</strong>${decision.detail ? `<em>${esc(decision.detail)}</em>` : ''}</span>
    <span class="decision-go" aria-hidden="true">→</span>
  </a>`;
}

// One card per asset on the overview; the decide page keeps every item.
function groupDecisions(decisions) {
  const byAsset = new Map();
  for (const decision of decisions) {
    if (!byAsset.has(decision.asset)) byAsset.set(decision.asset, []);
    byAsset.get(decision.asset).push(decision);
  }
  return [...byAsset.values()].map(items => {
    if (items.length === 1) return items[0];
    const selects = items.filter(d => d.type === 'select');
    const text = selects.length === items.length ? `选定 ${selects.length} 个视图的版本` : `${items.length} 项待决定`;
    return {...items[0], text, detail: items.map(d => d.text.replace(/^选定「(.+)」版本$/, '$1')).join(' · ')};
  });
}

function coverFor(key) {
  for (const group of S.overview?.scenes || []) for (const item of group.assets) if (item.asset.key === key) return item.cover?.thumb;
  return '';
}

function renderOverview() {
  const view = $('#studioView');
  document.title = `${S.project || '总览'} · Asset Review`;
  if (!S.project || !S.overview) {
    view.innerHTML = emptyStudio();
    return;
  }
  const {counts, scenes, decisions} = S.overview;
  view.innerHTML = `
    <header class="page-head">
      <div><span class="eyebrow">项目</span><h1>${esc(S.project)}</h1>
      <p class="page-meta">${counts.assets} 个资产 · ${counts.locked} 个已定稿 · ${counts.runtime} 个实机通过</p></div>
      <div class="page-actions"><button type="button" class="button primary" data-new-asset>新建资产</button></div>
    </header>
    ${decisions.length ? `<section class="block"><div class="block-head"><h2>待你决定</h2><span class="count-pill owner">${decisions.length}</span><a class="link" href="/#/decide" data-go="#/decide">全部</a></div>
      <div class="decision-strip">${groupDecisions(decisions).map(d => decisionRow(d, coverFor(d.asset))).join('')}</div></section>` : ''}
    ${scenes.length ? scenes.map(group => `<section class="block">
      <div class="block-head"><h2>${esc(group.scene || '未分场景')}</h2><span class="muted">${group.assets.length}</span></div>
      <div class="work-grid">${group.assets.map(assetCard).join('')}</div></section>`).join('') : emptyProject()}`;
}

function emptyProject() {
  return `<section class="empty-state"><div class="empty-art"></div><h2>这个项目还没有资产</h2>
    <p>资产是你要做出来的东西：一座音乐厅、一把剑、一个界面。为它声明需要哪些设计视图，设计稿、模型、贴图和实机截图就会按阶段汇集到这里。</p>
    <button type="button" class="button primary" data-new-asset>新建资产</button></section>`;
}

function emptyStudio() {
  return `<section class="empty-state hero"><div class="empty-art"></div><h1>从第一个资产开始</h1>
    <p>Asset Review 把散落的图片整理成「资产 → 设计视图 → 版本 → 定稿 → 制作阶段」。原文件留在你的项目目录里。</p>
    <button type="button" class="button primary" data-new-asset>新建资产</button></section>`;
}

function renderDecide() {
  const view = $('#studioView');
  document.title = `待决定 · ${S.project}`;
  const decisions = S.overview?.decisions || [];
  view.innerHTML = `<header class="page-head"><div><span class="eyebrow">${esc(S.project)}</span><h1>待你决定</h1>
    <p class="page-meta">只列出需要你拍板的事：选定设计版本、确认整套设计、阶段验收。其余由制作方推进。</p></div></header>
    ${decisions.length ? `<div class="decision-list">${decisions.map(d => decisionRow(d, coverFor(d.asset))).join('')}</div>`
      : '<section class="empty-state small"><h2>没有待决定的事项</h2><p>新的设计版本或待验收的阶段出现时会列在这里。</p></section>'}`;
}

// ------------------------------------------------------------------ asset page

function gateRow(gate) {
  if (gate.state === 'na') return '';
  return `<div class="gate ${esc(gate.state)}"><span class="gate-icon">${GATE_ICON[gate.state] || ''}</span><span class="gate-label">${esc(gate.label)}</span><span class="gate-detail">${esc(gate.detail || '')}</span></div>`;
}

function nextAction(detail) {
  const next = detail.next;
  const decision = next.decision;
  if (!decision) return '';
  if (decision.type === 'select') return `<button type="button" class="button primary" data-open-view="${esc(decision.view)}">去选择</button>`;
  if (decision.type === 'lock') return '<button type="button" class="button primary" data-lock>确认整套设计</button>';
  if (decision.type === 'accept') return `<button type="button" class="button primary" data-scroll-stage="${esc(decision.stage)}">去验收</button>`;
  return '';
}

function viewCard(view, detail) {
  const current = view.current;
  const chips = view.revisions.filter(r => r.state === 'candidate').map(r => `<span class="ver${r.selected ? ' selected' : ''}${r.locked ? ' locked' : ''}" title="${esc(r.label)}${r.note ? ` · ${esc(r.note)}` : ''}${r.selected ? ' · 已选用' : ''}${r.locked ? ' · 定稿' : ''}">${esc(r.label)}</span>`).join('');
  const status = {
    locked: `定稿 ${current?.label || ''}`,
    selected: `已选 ${current?.label || ''} · 待定稿`,
    updated: `有新版本`,
    choose: `${view.revisions.filter(r => r.state === 'candidate').length} 个候选待选`,
    missing: '缺少设计稿',
  }[view.status];
  return `<article class="view-card ${esc(view.status)}" data-view-card="${esc(view.view)}" tabindex="0" aria-label="${esc(view.label)}">
    <figure class="view-figure">${current ? media(current, 'preview') : `<div class="view-missing"><span>＋</span><small>拖入或上传${esc(view.label)}设计稿</small></div>`}
      ${current?.locked ? '<span class="lock-badge" title="定稿">定稿</span>' : ''}</figure>
    <div class="view-meta">
      <div class="view-title"><strong>${esc(view.label)}</strong>${view.required ? '' : '<span class="tag">可选</span>'}</div>
      <div class="view-status ${esc(view.status)}">${esc(status)}</div>
      ${chips ? `<div class="ver-row">${chips}</div>` : ''}
    </div>
  </article>`;
}

function stageCard(stage, detail) {
  const evidence = stage.evidence;
  const files = evidence.filter(item => item.thumb);
  const sources = evidence.filter(item => !item.thumb);
  const design = stage.stage === 'design';
  const canAccept = !design && stage.status !== 'accepted' && evidence.length > 0;
  const canReturn = !design && (stage.status === 'ready' || stage.status === 'accepted');
  return `<article class="stage-card ${esc(stage.status)}" id="stage-${esc(stage.stage)}" data-stage-card="${esc(stage.stage)}">
    <div class="stage-info">
      <div class="stage-name"><span class="stage-dot"></span><strong>${esc(stage.label)}</strong><span class="status-pill ${esc(stage.status)}">${esc(STATUS_TEXT[stage.status] || '')}</span></div>
      <p class="stage-hint">${esc(stage.hint)}</p>
      ${stage.summary ? `<p class="stage-summary">${esc(stage.summary)}</p>` : ''}
      ${stage.next_step && stage.status !== 'accepted' ? `<p class="stage-next">下一步：${esc(stage.next_step)}</p>` : ''}
      ${stage.accepted_at ? `<p class="stage-when">${design ? '定稿于' : '验收于'} ${esc(day(stage.accepted_at))}</p>` : ''}
      ${design ? '' : `<div class="stage-actions">
        ${canAccept ? `<button type="button" class="button ${stage.status === 'ready' ? 'primary' : ''}" data-accept="${esc(stage.stage)}">${stage.stage === 'engine' ? '实机验收' : '验收'}</button>` : ''}
        ${canReturn ? `<button type="button" class="button quiet" data-return="${esc(stage.stage)}">退回</button>` : ''}
        <button type="button" class="button quiet" data-upload-stage="${esc(stage.stage)}">添加证据</button>
      </div>
      <form class="return-form hidden" data-return-form="${esc(stage.stage)}"><input name="note" maxlength="500" placeholder="说明哪里要改" aria-label="退回说明"><button type="submit" class="button">退回</button></form>`}
    </div>
    <div class="stage-media">
      ${design ? designStrip(detail) : files.length ? `<div class="evidence-row">${files.slice(-4).map(item => `<button type="button" class="evidence" data-evidence="${esc(item.evidence_id)}" title="${esc(item.caption || item.rel || '')}">${media(item, 'thumb')}${item.kind !== 'image' ? `<span class="kind">${item.kind === 'timelapse' ? '延时' : '视频'}</span>` : ''}</button>`).join('')}${files.length > 4 ? `<span class="evidence-more">+${files.length - 4}</span>` : ''}</div>`
        : `<div class="evidence-missing" data-drop-stage="${esc(stage.stage)}">暂无素材 · 拖入截图或视频</div>`}
      ${sources.length ? `<ul class="source-list">${sources.map(item => `<li><span>来源</span>${esc(item.caption || item.source)}${item.caption && item.source ? ` <code>${esc(item.source)}</code>` : ''}</li>`).join('')}</ul>` : ''}
    </div>
  </article>`;
}

function designStrip(detail) {
  const views = detail.views.filter(view => view.current);
  if (!views.length) return '<div class="evidence-missing">还没有设计稿</div>';
  return `<div class="evidence-row">${views.slice(0, 4).map(view => `<button type="button" class="evidence" data-open-view="${esc(view.view)}" title="${esc(view.label)} ${esc(view.current.label)}">${media(view.current, 'thumb')}<span class="kind">${esc(view.label)}</span></button>`).join('')}</div>`;
}

function stepper(stages) {
  return `<ol class="stepper">${stages.map(stage => `<li class="${esc(stage.status)}"><a href="#stage-${esc(stage.stage)}" data-scroll-stage="${esc(stage.stage)}"><span class="step-dot"></span><span class="step-label">${esc(stage.label)}</span><span class="step-state">${esc(STATUS_TEXT[stage.status] || '')}</span></a></li>`).join('')}</ol>`;
}

function makingOf(detail) {
  return detail.making_of.map((chapter, index) => {
    const number = String(index + 1).padStart(2, '0');
    const when = [chapter.started_at ? day(chapter.started_at) : '', chapter.accepted_at ? `${chapter.stage === 'design' ? '定稿' : '验收'} ${day(chapter.accepted_at)}` : ''].filter(Boolean).join(' — ');
    const items = chapter.items.filter(item => item.thumb);
    const sources = chapter.items.filter(item => !item.thumb);
    const body = chapter.missing
      ? `<div class="chapter-missing">这一阶段还没有素材${chapter.status === 'todo' ? '' : '，进度已更新但没有截图或视频'}。</div>`
      : `<div class="chapter-grid count-${Math.min(items.length, 5)}">${items.map(item => `
          <figure class="chapter-item">${media(item, 'preview')}
            <figcaption>${chapter.stage === 'design' ? `${esc(item.view_label)} ${esc(item.label)}${item.locked ? ' · 定稿' : ''}` : esc(item.caption || (item.rel?.split('/').pop() || '').replace(/\.[^.]+$/, ''))}<time>${esc(day(item.created_at))}</time></figcaption>
          </figure>`).join('')}</div>
        ${sources.length ? `<ul class="source-list">${sources.map(item => `<li><span>来源</span>${esc(item.caption || item.source)}</li>`).join('')}</ul>` : ''}`;
    const locks = chapter.locks?.length ? `<div class="chapter-locks">${chapter.locks.map(lock => `<span class="lock-chip${lock.current ? ' current' : ''}">${esc(lock.name)} · ${esc(day(lock.created_at))}${lock.note ? ` · ${esc(lock.note)}` : ''}</span>`).join('')}</div>` : '';
    return `<section class="chapter ${chapter.missing ? 'missing' : ''}">
      <header class="chapter-head"><span class="chapter-no">${number}</span><div><h3>${esc(chapter.label)}</h3><p>${esc(chapter.summary || chapter.hint)}</p></div><time>${esc(when)}</time></header>
      ${locks}${body}</section>`;
  }).join('');
}

const EVENT_TEXT = {
  asset_created: () => '创建资产',
  asset_updated: () => '更新资产信息',
  view_added: d => `新增视图 ${d.label || d.view}${d.discovered ? '（目录发现）' : ''}`,
  view_updated: d => `调整视图 ${d.label || d.view}`,
  view_removed: d => `移除视图 ${d.view}`,
  revision_added: d => `${d.view} v${d.number} 加入${d.culled ? `，自动筛除：${d.culled}` : ''}`,
  revision_selected: d => `选用 ${d.view} v${d.number}`,
  revision_culled: d => `筛除 ${d.view} v${d.number}：${d.reason}`,
  revision_restored: d => `恢复 ${d.view} v${d.number}`,
  revision_archived: d => `清理 ${d.view} v${d.number}（可恢复）`,
  design_locked: d => `确认定稿 D${d.number}`,
  stage_updated: d => `${(S.meta?.stages.find(s => s.stage === d.stage) || {}).label || d.stage}：${STATUS_TEXT[d.old] || d.old} → ${STATUS_TEXT[d.new] || d.new}${d.next_step ? `（${d.next_step}）` : ''}`,
  evidence_added: d => `${(S.meta?.stages.find(s => s.stage === d.stage) || {}).label || d.stage} 新增${d.kind === 'source' ? '来源' : d.kind === 'video' ? '视频' : d.kind === 'timelapse' ? '延时' : '截图'}${d.caption ? `：${d.caption}` : ''}`,
  evidence_removed: d => `移除 ${d.stage} 证据`,
};
const ACTOR_TEXT = {owner: '你', agent: 'Agent', system: '自动'};

function cleanable(detail) {
  return detail.views.some(view => view.revisions.some(r => r.state === 'culled' || (detail.lock && r.state === 'candidate' && !r.locked && !r.selected)));
}

function renderAsset() {
  const detail = S.asset;
  if (!detail) return;
  const {asset, cover, gates, next} = detail;
  document.title = `${asset.name} · Asset Review`;
  const viewCount = detail.views.length;
  $('#studioView').innerHTML = `
    <nav class="crumbs"><a href="/#/" data-go="#/">${esc(asset.project)}</a>${asset.scene ? `<span>/</span><span>${esc(asset.scene)}</span>` : ''}</nav>
    <section class="asset-hero">
      <figure class="hero-media">${cover ? media(cover, 'preview') : `<div class="cover-empty large"><span>${esc(asset.name.charAt(0))}</span></div>`}
        ${cover ? `<figcaption><span>当前结果</span>${esc(cover.label)}${cover.caption ? ` · ${esc(cover.caption)}` : ''}</figcaption>` : ''}</figure>
      <div class="hero-info">
        <span class="eyebrow">${esc(asset.kind_label)}</span>
        <h1>${esc(asset.name)}</h1>
        ${asset.brief ? `<p class="brief">${esc(asset.brief)}</p>` : ''}
        <div class="gates">${gateRow(gates.visual)}${gateRow(gates.tech)}${gateRow(gates.runtime)}</div>
        <div class="next-card${next.owner ? ' owner' : ''}${next.done ? ' done' : ''}">
          <span class="next-label">${next.owner ? '需要你' : next.done ? '状态' : '下一步'}</span>
          <strong>${esc(next.text)}</strong>
          ${next.decision?.detail ? `<small>${esc(next.decision.detail)}</small>` : ''}
          ${nextAction(detail)}
        </div>
        <div class="hero-tools"><button type="button" class="button quiet" data-edit-asset>编辑资产</button><a class="button quiet" href="/api/production/lock?key=${encodeURIComponent(asset.key)}" target="_blank" rel="noopener" title="Coding Agent 读取的定稿 JSON">定稿 JSON</a></div>
      </div>
    </section>

    <nav class="section-tabs" aria-label="资产内容"><a href="#design" data-scroll="design">设计</a><a href="#stages" data-scroll="stages">制作</a><a href="#making" data-scroll="making">历程</a></nav>

    <section class="block" id="design">
      <div class="block-head"><h2>设计</h2><span class="muted">${viewCount} 个视图</span>
        ${detail.lock ? `<span class="lock-chip current">定稿 ${esc(detail.lock.name)} · ${esc(day(detail.lock.created_at))}</span>` : ''}
        <span class="spacer"></span>
        ${detail.decisions.some(d => d.type === 'lock') ? '<button type="button" class="button primary" data-lock>确认整套设计</button>' : ''}
        ${cleanable(detail) ? '<button type="button" class="button quiet" data-cleanup title="移到回收目录，可恢复">清理未采用</button>' : ''}
        <button type="button" class="button quiet" data-add-view>添加视图</button>
      </div>
      ${viewCount ? `<div class="view-grid">${detail.views.map(view => viewCard(view, detail)).join('')}</div>`
        : '<div class="empty-inline">还没有声明设计视图。按资产需要添加：正面、侧面、背面、俯视、局部细节……</div>'}
    </section>

    <section class="block" id="stages">
      <div class="block-head"><h2>制作</h2><span class="muted">${detail.progress.done}/${detail.progress.total} 完成</span></div>
      ${stepper(detail.stages)}
      <div class="stage-list">${detail.stages.map(stage => stageCard(stage, detail)).join('')}</div>
    </section>

    <section class="block making" id="making">
      <div class="block-head"><h2>制作历程</h2><span class="muted">从设计到实机，每一步的真实素材</span></div>
      ${makingOf(detail)}
      <details class="event-log"><summary>全部记录 <span class="muted">${detail.events.length}</span></summary>
        <ol>${detail.events.map(event => `<li><time>${esc(stamp(event.created_at))}</time><span class="actor ${esc(event.actor)}">${esc(ACTOR_TEXT[event.actor] || event.actor)}</span><span>${esc((EVENT_TEXT[event.action] || (() => event.action))(event.details || {}))}</span></li>`).join('')}</ol>
      </details>
    </section>`;
}

// ------------------------------------------------------------------ view studio

function viewerView() {
  return S.asset?.views.find(view => view.view === S.viewer?.view);
}

function openViewer(viewKey) {
  const view = S.asset?.views.find(item => item.view === viewKey);
  if (!view) return;
  const candidates = view.revisions.filter(r => r.state === 'candidate');
  const start = view.revisions.find(r => r.selected) || candidates[candidates.length - 1] || view.revisions[view.revisions.length - 1];
  S.viewer = {view: viewKey, revision: start?.revision_id || '', compare: '', mode: 'single', annotations: [], pinMode: false};
  $('#viewStudio').classList.remove('hidden');
  document.body.classList.add('viewer-open');
  renderViewer();
}

function closeViewer() {
  if (!S.viewer) return;
  S.viewer = null;
  $('#viewStudio').classList.add('hidden');
  $('#viewStudio').innerHTML = '';
  document.body.classList.remove('viewer-open');
}

function leaveViewer() {
  closeViewer();
  if (S.asset) go(`#/a/${S.asset.asset.key}`, {replace: true});
}

function renderViewer() {
  const view = viewerView();
  if (!view) return closeViewer();
  const all = view.revisions;
  const shown = all.filter(r => r.state === 'candidate' || S.showCulled || r.revision_id === S.viewer.revision);
  const culled = all.filter(r => r.state === 'culled');
  const current = all.find(r => r.revision_id === S.viewer.revision) || shown[shown.length - 1];
  if (current) S.viewer.revision = current.revision_id;
  const other = S.viewer.compare ? all.find(r => r.revision_id === S.viewer.compare) : null;
  const mode = other ? S.viewer.mode : 'single';
  const stage = !current ? `<div class="vs-empty"><p>「${esc(view.label)}」还没有设计稿</p><button type="button" class="button primary" data-upload-view="${esc(view.view)}">上传第一版</button></div>`
    : mode === 'side' ? `<div class="vs-side"><figure>${media(other, 'preview')}<figcaption>${esc(other.label)}</figcaption></figure><figure>${media(current, 'preview')}<figcaption>${esc(current.label)}</figcaption></figure></div>`
    : mode === 'wipe' ? `<div class="vs-wipe" data-cut="${S.viewer.cut ?? 50}"><img src="${esc(current.preview)}" alt=""><img class="top" src="${esc(other.preview)}" alt=""><span class="wipe-line"></span><span class="wipe-tag left">${esc(other.label)}</span><span class="wipe-tag right">${esc(current.label)}</span></div>`
    : `<div class="vs-figure${S.viewer.pinMode ? ' pinning' : ''}">${media(current, 'preview', 'draggable="false"')}<div class="vs-pins">${pinItems()}</div></div>`;
  $('#viewStudio').innerHTML = `
    <header class="vs-bar">
      <div class="vs-title"><span>${esc(S.asset.asset.name)}</span><strong>${esc(view.label)}</strong></div>
      <div class="vs-versions" role="tablist">${shown.map(r => `<button type="button" role="tab" class="vs-ver${r.revision_id === current?.revision_id ? ' active' : ''}${r.selected ? ' selected' : ''}${r.state === 'culled' ? ' culled' : ''}" data-rev="${esc(r.revision_id)}" aria-selected="${r.revision_id === current?.revision_id}">${esc(r.label)}${r.selected ? '<i class="sel-dot" title="已选用"></i>' : ''}</button>`).join('')}
        <button type="button" class="vs-ver add" data-upload-view="${esc(view.view)}" title="上传新版本">＋</button></div>
      <div class="vs-tools">
        ${all.filter(r => r.state === 'candidate').length > 1 ? `<div class="segmented" role="group" aria-label="对比"><button type="button" data-mode="single" aria-pressed="${mode === 'single'}">单张</button><button type="button" data-mode="side" aria-pressed="${mode === 'side'}">并排</button><button type="button" data-mode="wipe" aria-pressed="${mode === 'wipe'}">滑动</button></div>` : ''}
        <button type="button" class="icon-btn" data-close-viewer aria-label="关闭">×</button>
      </div>
    </header>
    <div class="vs-body">
      <section class="vs-stage" data-stage-area>${stage}</section>
      <aside class="vs-panel">${current ? viewerPanel(view, current, culled) : ''}</aside>
    </div>
    <nav class="vs-film">${shown.map(r => `<button type="button" class="film${r.revision_id === current?.revision_id ? ' active' : ''}${r.revision_id === other?.revision_id ? ' compare' : ''}${r.state === 'culled' ? ' culled' : ''}" data-rev="${esc(r.revision_id)}" title="${esc(r.label)}">${media(r, 'thumb')}<span>${esc(r.label)}${r.note ? ` · ${esc(r.note.split(/[，,。:：]/)[0].slice(0, 14))}` : ''}${r.selected ? ' ●' : ''}</span></button>`).join('')}</nav>`;
  applyStyles($('#viewStudio'));
  if (current && S.viewer.loadedFor !== current.revision_id) loadAnnotations(current).catch(console.error);
}

function viewerPanel(view, current, culled) {
  const isSelected = current.selected;
  const dims = current.width && current.height ? `${current.width}×${current.height}` : '';
  const others = view.revisions.filter(r => r.state === 'candidate' && r.revision_id !== current.revision_id);
  return `
    <div class="vs-head"><strong class="vs-ver-big">${esc(current.label)}</strong>
      ${isSelected ? `<span class="status-pill accepted">${current.locked ? '定稿' : '已选用'}</span>` : current.state === 'culled' ? `<span class="status-pill culled">已筛除</span>` : ''}</div>
    <p class="vs-sub">${esc(day(current.created_at))} · ${esc(ORIGIN_TEXT[current.origin] || current.origin)}${dims ? ` · ${dims}` : ''}</p>
    ${current.note ? `<p class="vs-note">${esc(current.note)}</p>` : ''}
    ${current.state === 'culled' ? `<p class="vs-reason">筛除原因：${esc(current.reason || '未说明')}</p>` : ''}
    ${current.changed ? '<p class="vs-warn">文件在加入后被改动过，定稿前请重新上传为新版本。</p>' : ''}
    <div class="vs-decide">
      ${isSelected ? `<button type="button" class="button wide" disabled>${current.locked ? '定稿中的版本' : '已选用此版本'}</button>`
        : `<button type="button" class="button primary wide" data-select="${esc(current.revision_id)}">选用 ${esc(current.label)} <kbd>↵</kbd></button>`}
      ${current.state === 'culled' ? `<button type="button" class="button quiet wide" data-restore="${esc(current.revision_id)}">恢复为候选</button>`
        : !isSelected && !current.locked ? `<button type="button" class="button quiet wide" data-cull="${esc(current.revision_id)}">不采用</button>` : ''}
    </div>
    ${others.length ? `<label class="vs-field"><span>对比</span><select data-compare-pick><option value="">选择另一个版本</option>${others.map(r => `<option value="${esc(r.revision_id)}"${r.revision_id === S.viewer.compare ? ' selected' : ''}>${esc(r.label)}${r.selected ? '（已选用）' : ''}</option>`).join('')}</select></label>` : ''}
    <label class="vs-field"><span>意见</span><textarea data-comment maxlength="10000" placeholder="这一版哪里要改">${esc(current.comment)}</textarea><small class="save-hint" data-comment-state></small></label>
    <div class="vs-field"><span>标注 <button type="button" class="text-btn${S.viewer.pinMode ? ' on' : ''}" data-pin-mode>${S.viewer.pinMode ? '点击图片放置…' : '在图上标注'}</button></span>
      <ol class="note-list">${noteItems()}</ol></div>
    ${culled.length ? `<button type="button" class="text-btn" data-toggle-culled>${S.showCulled ? '隐藏' : '显示'}已筛除的 ${culled.length} 个版本</button>` : ''}
    <p class="vs-keys"><kbd>←</kbd><kbd>→</kbd> 切换版本 · <kbd>↵</kbd> 选用 · <kbd>Esc</kbd> 返回</p>`;
}

async function loadAnnotations(revision) {
  S.viewer.loadedFor = revision.revision_id;
  const data = await api(`/api/annotations?collection=${encodeURIComponent(revision.collection)}&asset_id=${encodeURIComponent(revision.file_id)}&stale=0`);
  if (!S.viewer || S.viewer.revision !== revision.revision_id) return;
  S.viewer.annotations = data.annotations || [];
  // Patch pins and the list only, so typing in the comment box is never interrupted.
  const pins = $('#viewStudio .vs-pins');
  if (pins) { pins.innerHTML = pinItems(); applyStyles(pins); }
  const list = $('#viewStudio .note-list');
  if (list) list.innerHTML = noteItems();
}

function pinItems() {
  return S.viewer.annotations.map((note, index) => `<span class="pin${note.resolved ? ' resolved' : ''}" data-x="${note.x}" data-y="${note.y}" title="${esc(note.text)}">${index + 1}</span>`).join('');
}

function noteItems() {
  return S.viewer.annotations.map((note, index) => `<li class="${note.resolved ? 'resolved' : ''}"><span class="pin small">${index + 1}</span><span>${esc(note.text || '（无文字）')}</span><button type="button" class="text-btn" data-note-resolve="${esc(note.annotation_id)}">${note.resolved ? '重开' : '解决'}</button></li>`).join('') || '<li class="muted">还没有标注</li>';
}

function currentRevision() {
  const view = viewerView();
  return view?.revisions.find(r => r.revision_id === S.viewer?.revision);
}

function stepVersion(delta) {
  const view = viewerView();
  if (!view) return;
  const shown = view.revisions.filter(r => r.state === 'candidate' || S.showCulled);
  const index = shown.findIndex(r => r.revision_id === S.viewer.revision);
  const next = shown[Math.max(0, Math.min(shown.length - 1, index + delta))];
  if (next && next.revision_id !== S.viewer.revision) {
    S.viewer.revision = next.revision_id;
    if (S.viewer.compare === next.revision_id) S.viewer.compare = '';
    renderViewer();
  }
}

let commentTimer = null;
function saveComment(textarea) {
  const revision = currentRevision();
  if (!revision) return;
  clearTimeout(commentTimer);
  revision.comment = textarea.value;  // a re-render keeps what was typed
  const state = $('[data-comment-state]');
  if (state) state.textContent = '…';
  commentTimer = setTimeout(async () => {
    try {
      await post('/api/comment', {collection: revision.collection, asset_id: revision.file_id, comment: textarea.value});
      const hint = $('[data-comment-state]');
      if (hint) hint.textContent = '已保存';
    } catch (error) { fail(error); }
  }, 500);
}

async function selectRevision(revisionId) {
  const detail = await post('/api/production/select', {key: S.asset.asset.key, view: S.viewer.view, revision_id: revisionId});
  const view = detail.views.find(v => v.view === S.viewer.view);
  applyDetail(detail);
  const lockReady = detail.decisions.some(d => d.type === 'lock');
  toast(lockReady ? `已选用 ${view?.current?.label || ''} · 所有必需视图已就绪，可以确认整套设计` : `已选用 ${view?.label || ''} ${view?.current?.label || ''}`);
  // Move on to the next view that still needs a choice.
  const pending = detail.decisions.find(d => d.type === 'select' && d.view !== S.viewer.view);
  if (pending) setTimeout(() => go(`#/a/${detail.asset.key}/v/${pending.view}`, {replace: true}), 450);
}

// ------------------------------------------------------------------ dialogs

function openDialog(html, onSubmit) {
  const dialog = $('#dialog');
  dialog.innerHTML = `<form class="dialog" novalidate>${html}</form>`;
  dialog.classList.remove('hidden');
  const form = dialog.querySelector('form');
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    if (button) button.disabled = true;
    try {
      await onSubmit(form);
      closeDialog();
    } catch (error) {
      fail(error);
      if (button) button.disabled = false;
    }
  };
  form.querySelector('[autofocus]')?.focus();
  return form;
}

function closeDialog() {
  $('#dialog').classList.add('hidden');
  $('#dialog').innerHTML = '';
}

function lockDialog() {
  const detail = S.asset;
  const chosen = detail.views.filter(view => view.selected_revision_id);
  const missing = detail.views.filter(view => view.required && !view.selected_revision_id);
  const number = detail.locks.length + 1;
  openDialog(`
    <h2>确认整套设计 · D${number}</h2>
    <p class="dialog-lead">这些版本将成为制作依据。Coding Agent 只会读取定稿中的文件；之后改动设计需要再次定稿。</p>
    <div class="lock-preview">${chosen.map(view => `<figure>${media(view.current, 'thumb')}<figcaption><strong>${esc(view.label)}</strong> ${esc(view.current.label)}</figcaption></figure>`).join('')}</div>
    ${missing.length ? `<p class="vs-warn">还缺：${missing.map(v => esc(v.label)).join('、')}</p>` : ''}
    <label class="field"><span>定稿说明（可选）</span><input name="note" maxlength="300" placeholder="例如：采用拱顶方案，立面减少装饰"></label>
    <div class="dialog-actions"><button type="button" class="button quiet" data-dialog-cancel>取消</button><button type="submit" class="button primary"${missing.length ? ' disabled' : ''}>确认定稿 D${number}</button></div>`,
    async form => {
      const result = await post('/api/production/lock', {key: detail.asset.key, note: form.note.value});
      applyDetail(result);
      toast(`已定稿 ${result.lock.name}`);
    });
}

function chipToggles(name, options, chosen) {
  return `<div class="toggles" data-chips="${esc(name)}">${options.map(option => `<label class="toggle"><input type="checkbox" name="${esc(name)}" value="${esc(option.value)}"${chosen.includes(option.value) ? ' checked' : ''}${option.locked ? ' disabled' : ''}><span>${esc(option.label)}</span></label>`).join('')}</div>`;
}

async function assetDialog(existing) {
  S.meta = S.meta || await api('/api/production/meta');
  const meta = S.meta;
  const asset = existing?.asset;
  const kind = asset?.kind || 'environment';
  const kindInfo = meta.kinds.find(k => k.kind === kind);
  const scenes = [...new Set((S.overview?.scenes || []).map(group => group.scene).filter(Boolean))];
  const views = existing ? existing.views.map(v => v.view) : kindInfo.views;
  const viewOptions = [...meta.views.map(v => ({value: v.view, label: v.label})), ...(existing ? existing.views.filter(v => !meta.views.some(m => m.view === v.view)).map(v => ({value: v.view, label: v.label})) : [])];
  const libraryCollections = meta.collections.filter(c => !S.project || (c.group || '').split('/')[0].trim() === S.project);
  const form = openDialog(`
    <h2>${existing ? '编辑资产' : '新建资产'}</h2>
    <div class="field-row"><label class="field grow"><span>名称</span><input name="name" required maxlength="200" value="${esc(asset?.name || '')}" placeholder="例如：音乐厅" autofocus></label>
      <label class="field"><span>场景</span><input name="scene" list="sceneList" maxlength="200" value="${esc(asset?.scene || '')}" placeholder="可选"><datalist id="sceneList">${scenes.map(s => `<option value="${esc(s)}">`).join('')}</datalist></label></div>
    <label class="field"><span>类型</span><div class="segmented wide" data-kind-pick>${meta.kinds.map(k => `<button type="button" data-kind="${esc(k.kind)}" aria-pressed="${k.kind === kind}">${esc(k.label)}</button>`).join('')}</div></label>
    <div class="field"><span>需要的设计视图 <small>按资产实际需要勾选，未勾选的不会被要求</small></span>${chipToggles('views', viewOptions, views)}
      <div class="inline-add"><input name="customView" maxlength="40" placeholder="自定义视图，如：门厅细节"><button type="button" class="button quiet" data-add-custom-view>添加</button></div></div>
    <div class="field"><span>制作阶段</span>${chipToggles('stages', meta.stages.map(s => ({value: s.stage, label: s.label, locked: s.stage === 'design'})), asset?.stages || kindInfo.stages)}</div>
    <div class="field-row"><label class="field grow"><span>素材目录</span><select name="source_collection"><option value="">暂不关联</option>${(libraryCollections.length ? libraryCollections : meta.collections).map(c => `<option value="${esc(c.slug)}"${c.slug === asset?.source_collection ? ' selected' : ''}>${esc(c.label)}</option>`).join('')}</select></label>
      <label class="field"><span>子文件夹</span><input name="source_prefix" maxlength="200" value="${esc(asset?.source_prefix || '')}" placeholder="如 music-hall"></label></div>
    <p class="field-help">约定：<code>design/正面/</code> 或 <code>design/front_v2.png</code> 为设计版本；<code>model/</code> <code>uv/</code> <code>texture/</code> <code>rig/</code> <code>engine/</code> 为各阶段证据。放进去就会自动出现。</p>
    <label class="field"><span>设计要求（可选）</span><textarea name="brief" maxlength="4000" rows="3" placeholder="风格、尺度、必须保留的元素">${esc(asset?.brief || '')}</textarea></label>
    <div class="dialog-actions"><button type="button" class="button quiet" data-dialog-cancel>取消</button><button type="submit" class="button primary">${existing ? '保存' : '创建'}</button></div>`,
    async form => {
      const name = form.name.value.trim();
      if (!name) throw new Error('请填写名称');
      const chosenKind = form.querySelector('[data-kind-pick] [aria-pressed="true"]')?.dataset.kind || 'prop';
      const chosenViews = [...form.querySelectorAll('input[name="views"]:checked')].map(input => input.value);
      const chosenStages = [...form.querySelectorAll('input[name="stages"]')].filter(input => input.checked || input.disabled).map(input => input.value);
      const labels = Object.fromEntries([...form.querySelectorAll('input[name="views"]')].map(input => [input.value, input.nextElementSibling.textContent]));
      const fields = {name, scene: form.scene.value.trim(), kind: chosenKind, brief: form.brief.value, stages: chosenStages,
        source_collection: form.source_collection.value, source_prefix: form.source_prefix.value.trim()};
      if (existing) {
        let detail = await post('/api/production/asset', {action: 'update', key: asset.key, ...fields});
        for (const view of chosenViews.filter(v => !existing.views.some(e => e.view === v))) {
          detail = await post('/api/production/view', {action: 'add', key: asset.key, view, label: labels[view]});
        }
        for (const view of existing.views.filter(v => !chosenViews.includes(v.view))) {
          detail = await post('/api/production/view', view.revisions.length ? {action: 'update', key: asset.key, view: view.view, required: false} : {action: 'remove', key: asset.key, view: view.view});
        }
        applyDetail(detail);
        toast('已保存');
      } else {
        const detail = await post('/api/production/asset', {action: 'create', project: S.project || '我的项目', ...fields,
          views: chosenViews.map(view => ({key: view, label: labels[view]}))});
        await loadProjects();
        go(`#/a/${detail.asset.key}`);
        toast(`已创建 ${detail.asset.name}`);
      }
    });
  form.querySelector('[data-kind-pick]').onclick = event => {
    const button = event.target.closest('[data-kind]');
    if (!button) return;
    form.querySelectorAll('[data-kind]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    if (existing) return;
    const info = meta.kinds.find(k => k.kind === button.dataset.kind);
    form.querySelectorAll('input[name="views"]').forEach(input => { input.checked = info.views.includes(input.value); });
    form.querySelectorAll('input[name="stages"]').forEach(input => { if (!input.disabled) input.checked = info.stages.includes(input.value); });
  };
  form.querySelector('[data-add-custom-view]').onclick = () => {
    const input = form.customView;
    const label = input.value.trim();
    if (!label) return;
    const ascii = label.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const value = ascii || label;  // the server derives a stable key from a Chinese name
    form.querySelector('[data-chips="views"]').insertAdjacentHTML('beforeend', `<label class="toggle"><input type="checkbox" name="views" value="${esc(value)}" checked><span>${esc(label)}</span></label>`);
    input.value = '';
  };
}

function newProjectDialog() {
  toggleProjectMenu(false);
  openDialog(`<h2>新项目</h2><p class="dialog-lead">项目在创建第一个资产时生效。素材库里按「项目 / 场景」登记的目录会自动归到同名项目下。</p>
    <label class="field"><span>项目名称</span><input name="project" required maxlength="200" autofocus placeholder="例如：Nameless Reach"></label>
    <div class="dialog-actions"><button type="button" class="button quiet" data-dialog-cancel>取消</button><button type="submit" class="button primary">继续</button></div>`,
    async form => {
      const name = form.project.value.trim();
      if (!name) throw new Error('请填写项目名称');
      S.project = name;
      writePref(PROJECT_KEY, name);
      if (!S.projects.some(p => p.name === name)) S.projects.push({name, assets: 0, collections: 0});
      S.overview = {project: name, scenes: [], decisions: [], counts: {assets: 0, locked: 0, runtime: 0, decisions: 0}};
      setMode('studio');
      history.pushState(null, '', '/#/');
      renderOverview();
      renderSidebar();
      setTimeout(() => assetDialog(null).catch(fail), 50);
    });
}

function addViewDialog() {
  openDialog(`<h2>添加视图</h2>
    <div class="toggles" data-preset>${(S.meta?.views || []).filter(v => !S.asset.views.some(e => e.view === v.view)).map(v => `<button type="button" class="toggle-btn" data-preset-view="${esc(v.view)}" data-label="${esc(v.label)}">${esc(v.label)}</button>`).join('')}</div>
    <div class="field-row"><label class="field grow"><span>名称</span><input name="label" required maxlength="60" autofocus placeholder="例如：舞台局部"></label>
      <label class="field"><span>标识</span><input name="view" maxlength="40" placeholder="如 stage-detail"></label></div>
    <label class="check-field"><input type="checkbox" name="required" checked> 定稿前必须有</label>
    <div class="dialog-actions"><button type="button" class="button quiet" data-dialog-cancel>取消</button><button type="submit" class="button primary">添加</button></div>`,
    async form => {
      const label = form.label.value.trim();
      if (!label) throw new Error('请填写视图名称');
      const view = form.view.value.trim() || label;
      applyDetail(await post('/api/production/view', {action: 'add', key: S.asset.asset.key, view, label, required: form.required.checked}));
    });
  $('#dialog [data-preset]').onclick = event => {
    const button = event.target.closest('[data-preset-view]');
    if (!button) return;
    const form = $('#dialog form');
    form.label.value = button.dataset.label;
    form.view.value = button.dataset.presetView;
  };
}

async function evidenceViewer(evidenceId) {
  const item = S.asset.stages.flatMap(stage => stage.evidence).find(e => e.evidence_id === evidenceId);
  if (!item) return;
  const stage = S.asset.stages.find(s => s.stage === item.stage);
  openDialog(`<div class="evidence-view">${item.media_type === 'video' ? `<video src="${esc(item.file)}" controls autoplay playsinline></video>` : `<img src="${esc(item.preview)}" alt="">`}</div>
    <div class="evidence-caption"><div><strong>${esc(stage?.label || '')}</strong> · ${esc(item.caption || item.rel || '')}<br><small class="muted">${esc(stamp(item.created_at))} · ${esc(ORIGIN_TEXT[item.origin] || item.origin)}</small></div>
    <div class="dialog-actions"><a class="button quiet" href="${esc(item.file)}" target="_blank" rel="noopener">原文件</a><button type="button" class="button quiet" data-remove-evidence="${esc(item.evidence_id)}">移出此阶段</button><button type="submit" class="button">关闭</button></div></div>`,
    async () => {});
  $('#dialog .dialog').classList.add('wide');
}

async function cleanupDialog() {
  const plan = await api(`/api/production/cleanup?asset=${encodeURIComponent(S.asset.asset.key)}`);
  if (!plan.count) { toast('没有可清理的版本'); return; }
  openDialog(`<h2>清理未采用的版本</h2>
    <p class="dialog-lead">这些文件会移到素材目录下的 <code>.asset-review-trash/</code>，记录保留、随时可恢复。定稿、已选用、已通过和作为证据的文件不会被动。</p>
    <ul class="cleanup-list">${plan.items.map(item => `<li><label><input type="checkbox" name="rev" value="${esc(item.revision_id)}" checked>${media(item, 'thumb')}<span><strong>${esc(item.view)} v${item.number}</strong><small>${esc(item.reason)}</small></span></label></li>`).join('')}</ul>
    <div class="dialog-actions"><button type="button" class="button quiet" data-dialog-cancel>取消</button><button type="submit" class="button primary">清理 ${plan.count} 个</button></div>`,
    async form => {
      const ids = [...form.querySelectorAll('input[name="rev"]:checked')].map(input => input.value);
      if (!ids.length) return;
      const result = await post('/api/production/cleanup', {revision_ids: ids});
      toast(`已清理 ${result.moved.length} 个版本，可在记录中恢复`);
      await loadAsset(S.asset.asset.key);
    });
}

// ------------------------------------------------------------------ library assign

async function renderAssign(file) {
  const row = $('#assignRow');
  if (!row || !file?.asset_id) return;
  const data = await api(`/api/production/file?file_id=${encodeURIComponent(file.asset_id)}`);
  const links = data.links.map(link => `<a href="/#/a/${esc(link.asset)}${link.view ? `/v/${esc(link.view)}` : `/s/${esc(link.stage)}`}" data-go="#/a/${esc(link.asset)}${link.view ? `/v/${esc(link.view)}` : `/s/${esc(link.stage)}`}">${esc(link.asset_name)} · ${esc(link.label)}</a>`).join('、');
  const targets = data.targets.filter(t => !S.project || t.project === S.project);
  row.classList.remove('hidden');
  // A file used by an asset is decided on the asset page, not here.
  $('#reviewStatus').classList.toggle('hidden', data.links.length > 0);
  $('#reviewQuestion').classList.toggle('hidden', data.links.length > 0);
  row.innerHTML = links ? `<span class="assign-label">已用于</span><span class="assign-links">${links}</span>`
    : targets.length ? `<span class="assign-label">归入资产</span><select data-assign aria-label="归入资产"><option value="">选择视图或阶段…</option>${targets.map(t => `<optgroup label="${esc(t.name)}">${t.views.map(v => `<option value="view|${esc(t.key)}|${esc(v.view)}">设计 · ${esc(v.label)}（新版本）</option>`).join('')}${t.stages.filter(s => s.stage !== 'design').map(s => `<option value="stage|${esc(t.key)}|${esc(s.stage)}">${esc(s.label)} · 证据</option>`).join('')}</optgroup>`).join('')}</select>`
    : '<span class="assign-label muted">新建资产后可把文件归入设计视图或制作阶段</span>';
  const select = row.querySelector('[data-assign]');
  if (select) select.onchange = async () => {
    const [type, key, target] = select.value.split('|');
    if (!key) return;
    try {
      const payload = type === 'view'
        ? await post('/api/production/revision', {action: 'attach', key, view: target, collection: file.collection, file_id: file.asset_id})
        : await post('/api/production/evidence', {action: 'attach', key, stage: target, collection: file.collection, file_id: file.asset_id});
      const name = payload.asset.name;
      toast(`已归入 ${name}`);
      renderAssign(file).catch(console.error);
      loadOverview().then(renderSidebar).catch(console.error);
    } catch (error) { fail(error); select.value = ''; }
  };
}

// ------------------------------------------------------------------ events

document.addEventListener('click', event => {
  const target = event.target;
  const goLink = target.closest('[data-go]');
  if (goLink && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
    event.preventDefault();
    if (!$('#modal').classList.contains('hidden')) $('#close').click();
    go(goLink.dataset.go);
    return;
  }
  const nav = target.closest('[data-nav]');
  if (nav) {
    event.preventDefault();
    go(nav.dataset.nav === 'decide' ? '#/decide' : '#/');
    return;
  }
  if (target.closest('#projectSwitcher')) { toggleProjectMenu(); return; }
  const pick = target.closest('[data-project-pick]');
  if (pick) { switchProject(pick.dataset.projectPick).catch(fail); return; }
  if (target.closest('[data-project-new]')) { newProjectDialog(); return; }
  if (!target.closest('#projectMenu')) toggleProjectMenu(false);
  if (target.closest('[data-new-asset]') || target.closest('#newAsset')) { assetDialog(null).catch(fail); return; }
  if (target.closest('[data-edit-asset]')) { assetDialog(S.asset).catch(fail); return; }
  if (target.closest('[data-dialog-cancel]') || target.id === 'dialog') { closeDialog(); return; }
  if (target.closest('[data-lock]')) { lockDialog(); return; }
  if (target.closest('[data-add-view]')) { (S.meta ? Promise.resolve() : api('/api/production/meta').then(m => { S.meta = m; })).then(addViewDialog).catch(fail); return; }
  if (target.closest('[data-cleanup]')) { cleanupDialog().catch(fail); return; }
  const scroll = target.closest('[data-scroll]');
  if (scroll) { event.preventDefault(); document.getElementById(scroll.dataset.scroll)?.scrollIntoView({behavior: 'smooth', block: 'start'}); return; }
  const scrollStage = target.closest('[data-scroll-stage]');
  if (scrollStage) { event.preventDefault(); document.getElementById(`stage-${scrollStage.dataset.scrollStage}`)?.scrollIntoView({behavior: 'smooth', block: 'center'}); return; }
  const openView = target.closest('[data-open-view]') || (!target.closest('button,a') && target.closest('[data-view-card]'));
  if (openView && S.asset) { go(`#/a/${S.asset.asset.key}/v/${openView.dataset.openView || openView.dataset.viewCard}`); return; }
  const evidence = target.closest('[data-evidence]');
  if (evidence) { evidenceViewer(evidence.dataset.evidence).catch(fail); return; }
  const removeEvidence = target.closest('[data-remove-evidence]');
  if (removeEvidence) {
    post('/api/production/evidence', {action: 'remove', key: S.asset.asset.key, evidence_id: removeEvidence.dataset.removeEvidence})
      .then(detail => { closeDialog(); applyDetail(detail); toast('已移出，原文件未改动'); }).catch(fail);
    return;
  }
  const accept = target.closest('[data-accept]');
  if (accept) {
    post('/api/production/stage', {key: S.asset.asset.key, stage: accept.dataset.accept, status: 'accepted'})
      .then(detail => { applyDetail(detail); toast(accept.dataset.accept === 'engine' ? '实机已验收' : '已验收'); }).catch(fail);
    return;
  }
  const back = target.closest('[data-return]');
  if (back) {
    const form = document.querySelector(`[data-return-form="${back.dataset.return}"]`);
    form.classList.toggle('hidden');
    form.note.focus();
    return;
  }
  const uploadStage = target.closest('[data-upload-stage]');
  if (uploadStage) {
    pickFiles('image/*,video/*').then(files => uploadMany(files, {stage: uploadStage.dataset.uploadStage})).catch(fail);
    return;
  }
  const uploadView = target.closest('[data-upload-view]');
  if (uploadView) {
    pickFiles('image/*', false).then(files => uploadMany(files, {view: uploadView.dataset.uploadView})).catch(fail);
    return;
  }
  // View studio
  if (target.closest('[data-close-viewer]')) { leaveViewer(); return; }
  const rev = target.closest('[data-rev]');
  if (rev && S.viewer) {
    if (event.altKey || event.shiftKey) S.viewer.compare = rev.dataset.rev === S.viewer.revision ? '' : rev.dataset.rev;
    else { S.viewer.revision = rev.dataset.rev; if (S.viewer.compare === rev.dataset.rev) S.viewer.compare = ''; }
    if (S.viewer.compare && S.viewer.mode === 'single') S.viewer.mode = 'side';
    renderViewer();
    return;
  }
  const mode = target.closest('#viewStudio [data-mode]');
  if (mode && S.viewer) {
    S.viewer.mode = mode.dataset.mode;
    if (S.viewer.mode !== 'single' && !S.viewer.compare) {
      const view = viewerView();
      const others = view.revisions.filter(r => r.state === 'candidate' && r.revision_id !== S.viewer.revision);
      S.viewer.compare = (others.find(r => r.selected) || others[others.length - 1])?.revision_id || '';
    }
    renderViewer();
    return;
  }
  const select = target.closest('[data-select]');
  if (select) { selectRevision(select.dataset.select).catch(fail); return; }
  const cull = target.closest('[data-cull]');
  if (cull) {
    post('/api/production/revision', {action: 'cull', key: S.asset.asset.key, revision_id: cull.dataset.cull, reason: '不采用'})
      .then(detail => { applyDetail(detail); stepVersion(-1); toast('已移出候选，随时可恢复'); }).catch(fail);
    return;
  }
  const restore = target.closest('[data-restore]');
  if (restore) { post('/api/production/revision', {action: 'restore', key: S.asset.asset.key, revision_id: restore.dataset.restore}).then(applyDetail).catch(fail); return; }
  if (target.closest('[data-toggle-culled]')) { S.showCulled = !S.showCulled; renderViewer(); return; }
  if (target.closest('[data-pin-mode]')) { S.viewer.pinMode = !S.viewer.pinMode; renderViewer(); return; }
  const resolve = target.closest('[data-note-resolve]');
  if (resolve) {
    const note = S.viewer.annotations.find(n => n.annotation_id === resolve.dataset.noteResolve);
    const revision = currentRevision();
    post('/api/annotation', {action: 'update', collection: revision.collection, asset_id: revision.file_id, annotation_id: note.annotation_id, resolved: !note.resolved})
      .then(() => { S.viewer.loadedFor = ''; renderViewer(); }).catch(fail);
    return;
  }
  const figure = target.closest('.vs-figure.pinning img');
  if (figure && S.viewer?.pinMode) placePin(event, figure);
});

document.addEventListener('submit', event => {
  const form = event.target.closest('[data-return-form]');
  if (!form) return;
  event.preventDefault();
  post('/api/production/stage', {key: S.asset.asset.key, stage: form.dataset.returnForm, status: 'wip', next_step: form.note.value || '按意见修改'})
    .then(detail => { applyDetail(detail); toast('已退回'); }).catch(fail);
});

document.addEventListener('input', event => {
  if (event.target.matches('#viewStudio [data-comment]')) saveComment(event.target);
  if (event.target.matches('#viewStudio [data-compare-pick]')) {
    S.viewer.compare = event.target.value;
    if (S.viewer.compare && S.viewer.mode === 'single') S.viewer.mode = 'side';
    if (!S.viewer.compare) S.viewer.mode = 'single';
    renderViewer();
  }
});

document.addEventListener('pointermove', event => {
  const wipe = event.target.closest?.('.vs-wipe');
  if (!wipe || !S.viewer || (event.buttons !== 1 && event.pointerType !== 'mouse')) return;
  const rect = wipe.getBoundingClientRect();
  S.viewer.cut = Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100));
  wipe.style.setProperty('--cut', `${S.viewer.cut}%`);
});

function placePin(event, image) {
  const rect = image.getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width;
  const y = (event.clientY - rect.top) / rect.height;
  const revision = currentRevision();
  const holder = image.parentElement;
  holder.insertAdjacentHTML('beforeend', `<form class="pin-form" data-x="${x}" data-y="${y}"><input maxlength="4000" placeholder="写下这里要改什么" aria-label="标注"><button class="button primary" type="submit">添加</button></form>`);
  const form = holder.querySelector('.pin-form');
  applyStyles(holder);
  form.querySelector('input').focus();
  form.onsubmit = submit => {
    submit.preventDefault();
    post('/api/annotation', {action: 'create', collection: revision.collection, asset_id: revision.file_id, kind: 'point', x, y, text: form.querySelector('input').value})
      .then(() => { S.viewer.pinMode = false; S.viewer.loadedFor = ''; renderViewer(); }).catch(fail);
  };
}

async function uploadMany(files, target) {
  if (!files.length || !S.asset) return;
  let detail = null;
  for (const file of files) {
    toast(`正在上传 ${file.name}…`);
    detail = await upload(S.asset.asset.key, target, file);
  }
  if (detail) {
    applyDetail(detail);
    if (target.view && S.viewer) {
      const view = detail.views.find(v => v.view === target.view);
      const latest = view?.revisions[view.revisions.length - 1];
      if (latest) { S.viewer.revision = latest.revision_id; renderViewer(); }
    }
    toast(target.view ? '新版本已加入' : `已添加 ${files.length} 项证据`);
  }
}

// Drag files onto a view card (new revision) or a stage card (evidence).
document.addEventListener('dragover', event => {
  const zone = event.target.closest?.('[data-view-card],[data-stage-card],#viewStudio .vs-stage');
  if (!zone || !event.dataTransfer?.types?.includes('Files')) return;
  event.preventDefault();
  zone.classList.add('drop-over');
});
document.addEventListener('dragleave', event => event.target.closest?.('.drop-over')?.classList.remove('drop-over'));
document.addEventListener('drop', event => {
  const zone = event.target.closest?.('[data-view-card],[data-stage-card],#viewStudio .vs-stage');
  if (!zone || !event.dataTransfer?.files?.length) return;
  event.preventDefault();
  zone.classList.remove('drop-over');
  const files = [...event.dataTransfer.files];
  const target = zone.dataset.viewCard ? {view: zone.dataset.viewCard} : zone.dataset.stageCard ? {stage: zone.dataset.stageCard} : {view: S.viewer?.view};
  if (target.stage === 'design') return;
  uploadMany(files, target).catch(fail);
});

document.addEventListener('keydown', event => {
  if (!$('#dialog').classList.contains('hidden')) {
    if (event.key === 'Escape') closeDialog();
    return;
  }
  if (!S.viewer) {
    if (event.key === 'Escape') toggleProjectMenu(false);
    return;
  }
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName);
  if (event.key === 'Escape') {
    if (editing) { event.target.blur(); return; }
    if (S.viewer.pinMode) { S.viewer.pinMode = false; renderViewer(); return; }
    leaveViewer();
    return;
  }
  if (editing || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === 'ArrowRight') { event.preventDefault(); stepVersion(1); }
  else if (event.key === 'ArrowLeft') { event.preventDefault(); stepVersion(-1); }
  else if (event.key === 'Enter') {
    const revision = currentRevision();
    if (revision && !revision.selected) { event.preventDefault(); selectRevision(revision.revision_id).catch(fail); }
  }
}, true);

window.addEventListener('popstate', () => {
  if (location.hash.startsWith('#/') || location.pathname === '/') route().catch(fail);
});
window.addEventListener('ar:file', event => { renderAssign(event.detail).catch(console.error); });
window.addEventListener('ar:library-ready', () => {
  renderSidebar();
  if (S.project && document.body.dataset.mode === 'studio') window.AR.library.setProject(S.project).catch(console.error);
});

window.AR = Object.assign(window.AR || {}, {
  showLibrary,
  paletteCommands: () => [
    {label: '总览', alias: 'overview home', run: () => go('#/')},
    {label: '待决定', alias: 'decide decisions', run: () => go('#/decide')},
    {label: '新建资产', alias: 'new asset create', run: () => assetDialog(null)},
    ...(S.overview?.scenes || []).flatMap(group => group.assets.map(item => ({label: `资产：${item.asset.name}`, alias: `asset ${item.asset.key}`, run: () => go(`#/a/${item.asset.key}`)}))),
    ...S.projects.map(project => ({label: `项目：${project.name}`, alias: 'project switch', run: () => switchProject(project.name)})),
  ],
});

(async () => {
  try {
    await loadProjects();
    const libraryLink = location.pathname.startsWith('/c/') || new URLSearchParams(location.search).has('asset');
    if (libraryLink && !location.hash) {
      setMode('library');
      await loadOverview();
      renderSidebar();
      return;
    }
    await route();
    if (S.project) window.AR?.library?.setProject?.(S.project)?.catch?.(console.error);
  } catch (error) { fail(error); }
})();
})();
