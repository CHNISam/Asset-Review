let state = {
  collections: [],
  images: [],
  families: [],
  filtered: [],
  current: 0,
  csrf: '',
  selected: new Set(),
  active: '',
  reviewState: null,
  compareAssets: [],
  compareMode: 'side',
  annotations: [],
  annotationMode: null,
  annotationStart: null,
  compareLinked: true,
  compareTransforms: [],
  compareDrag: null,
  catalogGeneration: 0,
  view: 'gallery',
  activityEvents: [],
  savedViews: [],
  activeSavedView: '',
  paletteMatches: [],
  paletteIndex: 0,
  workspaceTab: 'gallery',
  galleryFilterBeforeApproved: 'all',
  scan: {},
  scope: {kind: 'collection', project: '', scene: ''},
  scopeSlugs: [],
  scopeRequest: 0,
  selectionMode: false,
  selectionAnchor: null,
  selectionAnchorKey: '',
  // Ephemeral presentation state only: quick review still reads and writes
  // the same in-memory gallery rows and the native review endpoints.
  quickReview: {open: false, keys: [], index: 0, scopeRequest: 0, runId: 0, saving: false, lastAction: null, commentTimer: null, commentPromise: null},
  quickReviewRun: 0,
  quickReviewSaveRequest: 0,
  quickReviewCommentRequest: 0,
};

const $ = selector => document.querySelector(selector);
const grid = $('#grid');
const modal = $('#modal');
const compareModal = $('#compareModal');
const commandPalette = $('#commandPalette');
const SAVED_VIEWS_KEY = 'asset-viewer.saved-views.v1';
const esc = value => String(value).replace(/[&<>\"]/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[char]));
const keyFor = asset => `${asset.collection || state.active}:${asset.asset_id || asset.rel}`;
const assetSrc = asset => asset.preview || asset.thumb;

const USE_LABELS = {
  reference: '参考',
  design: '设计',
  runtime: '实机',
  asset: '资产',
  uncategorized: '未分类',
};

function useLabel(value) {
  return USE_LABELS[value] || USE_LABELS.uncategorized;
}

function collectionTaxonomy(collectionOrSlug) {
  const collection = typeof collectionOrSlug === 'string'
    ? state.collections.find(item => item.slug === collectionOrSlug) || {slug: collectionOrSlug}
    : (collectionOrSlug || {});
  const parts = String(collection.group || '').split('/').map(part => part.trim()).filter(Boolean);
  const project = parts[0] || collection.source_project || '未分类项目';
  const scene = parts[1] || '未分类';
  // A collection may explicitly declare its content use as the final group
  // segment (for example, "Project / Scene / Reference").  Names and file
  // paths are deliberately not used as hints: unknown material stays
  // uncategorized until a person gives it a clear home.
  const declaredUse = String(collection.content_use || collection.use || parts[parts.length - 1] || '').trim().toLowerCase();
  const declaredUses = {
    reference: 'reference', references: 'reference', '参考': 'reference',
    design: 'design', designs: 'design', '设计': 'design',
    runtime: 'runtime', '实机': 'runtime',
    asset: 'asset', assets: 'asset', '资产': 'asset',
    uncategorized: 'uncategorized', '未分类': 'uncategorized',
  };
  const use = declaredUses[declaredUse] || 'uncategorized';
  const topicParts = use === 'uncategorized' ? parts.slice(2) : parts.slice(2, -1);
  return {project, scene, topic: topicParts.join(' · ') || collection.label || '未分类', use, note: String(collection.note || '').trim()};
}

function assetTaxonomy(asset) {
  return collectionTaxonomy(asset?.collection);
}

function collectionBySlug(slug) {
  return state.collections.find(collection => collection.slug === slug) || null;
}

function isSingleCollectionScope() {
  return state.scopeSlugs.length <= 1;
}

function loadSavedViews() {
  try {
    const parsed = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || '[]');
    state.savedViews = Array.isArray(parsed) ? parsed.filter(item => item && typeof item.name === 'string' && typeof item.id === 'string') : [];
  } catch {
    state.savedViews = [];
  }
}

function persistSavedViews() {
  localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(state.savedViews));
}

function renderViewMenu() {
  const select = $('#view');
  const saved = state.savedViews.map(item => `<option value="saved:${esc(item.id)}">${esc(item.name)}</option>`).join('');
  select.innerHTML = '<option value="gallery">视图…</option><option value="activity">审核记录</option>' +
    (saved ? `<optgroup label="已保存视图">${saved}</optgroup>` : '');
  select.value = state.activeSavedView ? `saved:${state.activeSavedView}` : state.view;
}

function clearActiveSavedView() {
  if (!state.activeSavedView) return;
  state.activeSavedView = '';
  renderViewMenu();
}

function saveCurrentView() {
  const name = window.prompt('为此视图命名');
  if (!name || !name.trim()) return;
  const trimmed = name.trim().slice(0, 80);
  const existing = state.savedViews.find(item => item.name.toLowerCase() === trimmed.toLowerCase());
  const record = {
    id: existing?.id || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    name: trimmed,
    search: $('#search').value,
    filter: $('#filter').value,
    sort: $('#sort').value,
  };
  state.savedViews = existing ? state.savedViews.map(item => item.id === existing.id ? record : item) : [...state.savedViews, record];
  state.activeSavedView = record.id;
  persistSavedViews();
  renderViewMenu();
}

function applySavedView(id) {
  const record = state.savedViews.find(item => item.id === id);
  if (!record) return;
  state.activeSavedView = record.id;
  $('#search').value = record.search || '';
  $('#filter').value = record.filter || 'all';
  $('#sort').value = record.sort || 'newest';
  state.view = 'gallery';
  state.workspaceTab = 'gallery';
  $('#galleryView').classList.remove('hidden');
  $('#activityView').classList.add('hidden');
  applyFilter();
  renderViewMenu();
  renderWorkspaceState();
  updateCollectionActions();
}

function deleteActiveSavedView() {
  if (!state.activeSavedView) return;
  state.savedViews = state.savedViews.filter(item => item.id !== state.activeSavedView);
  state.activeSavedView = '';
  persistSavedViews();
  renderViewMenu();
}

function collectionFromLocation() {
  const match = location.pathname.match(/^\/c\/([^/]+)$/);
  if (match) return decodeURIComponent(match[1]);
  return new URLSearchParams(location.search).get('collection') || '';
}

function collectionUrl(slug) {
  return '/c/' + encodeURIComponent(slug);
}

function assetFromLocation() {
  return new URLSearchParams(location.search).get('asset') || '';
}

function assetUrl(asset) {
  return collectionUrl(asset.collection) + '?asset=' + encodeURIComponent(asset.asset_id || asset.rel);
}

async function session() {
  const response = await fetch('/api/session', {cache: 'no-store'});
  if (!response.ok) throw new Error('Session request failed');
  const data = await response.json();
  state.csrf = data.csrf;
}

async function apiPost(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Asset-Viewer-CSRF': state.csrf},
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function fetchGallery(slug, force = false) {
  const params = new URLSearchParams();
  if (slug) params.set('collection', slug);
  if (force) params.set('refresh', '1');
  const response = await fetch('/api/gallery' + (params.toString() ? '?' + params : ''), {cache: 'no-store'});
  if (!response.ok) throw new Error('图库请求失败');
  return response.json();
}

function resetSelectionMode() {
  state.selected.clear();
  state.selectionAnchor = null;
  state.selectionAnchorKey = '';
  state.selectionMode = false;
  document.body.classList.remove('selection-mode');
  const toggle = $('#selectionMode');
  if (toggle) {
    toggle.classList.remove('active');
    toggle.setAttribute('aria-pressed', 'false');
    toggle.textContent = '多选';
  }
}

function applyGalleryScope(payloads, scope) {
  const first = payloads[0] || {};
  state.collections = first.collections || state.collections;
  state.images = payloads.flatMap(payload => (payload.images || []).map(asset => ({
    ...asset,
    collection: asset.collection || payload.active,
  })));
  state.families = payloads.flatMap(payload => (payload.families || []).map(family => ({
    ...family,
    collection: family.collection || payload.active,
  })));
  state.scopeSlugs = [...new Set(payloads.map(payload => payload.active).filter(Boolean))];
  state.active = state.scopeSlugs.includes(scope.active) ? scope.active : (state.scopeSlugs[0] || '');
  state.scope = {
    kind: scope.kind || 'collection',
    project: scope.project || '',
    scene: scope.scene || '',
  };
  state.reviewState = state.scopeSlugs.length === 1 ? first.review_state : null;
  state.scan = state.scopeSlugs.length === 1 ? (first.scan || {}) : {};
  state.catalogGeneration = Number((first.scan || {}).generation || 0);
  resetSelectionMode();
  renderCollections(state.active);
  renderFacetControls();
  applyFilter();
  updateBulk();
  updateReviewState();
}

async function load(requested, force = false) {
  if (isQuickReviewOpen()) closeQuickReview({refresh: false});
  const requestedAsset = assetFromLocation();
  const desired = requested || state.active || collectionFromLocation();
  const request = ++state.scopeRequest;
  const data = await fetchGallery(desired, force);
  if (request !== state.scopeRequest) return;
  const active = data.active;
  const taxonomy = collectionTaxonomy((data.collections || []).find(collection => collection.slug === active) || {slug: active});
  applyGalleryScope([data], {kind: 'collection', project: taxonomy.project, scene: taxonomy.scene, active});
  if (active && location.pathname !== collectionUrl(active)) history.replaceState(null, '', collectionUrl(active));
  if (requestedAsset) {
    const index = state.filtered.findIndex(asset => (asset.asset_id || asset.rel) === requestedAsset);
    if (index >= 0) openAt(index, false);
  }
}

async function loadScope(slugs, scope, force = false) {
  if (isQuickReviewOpen()) closeQuickReview({refresh: false});
  const unique = [...new Set(slugs)].filter(Boolean);
  if (!unique.length) return;
  const request = ++state.scopeRequest;
  const payloads = await Promise.all(unique.map(slug => fetchGallery(slug, force)));
  if (request !== state.scopeRequest) return;
  const active = unique.includes(state.active) ? state.active : unique[0];
  applyGalleryScope(payloads, {...scope, active});
  if (state.active && location.pathname !== collectionUrl(state.active)) history.replaceState(null, '', collectionUrl(state.active));
}

async function reloadCurrentScope(force = true) {
  const slugs = state.scopeSlugs.length ? state.scopeSlugs : [state.active];
  if (slugs.length <= 1) return load(slugs[0], force);
  return loadScope(slugs, state.scope, force);
}

function activeCollectionRecord() {
  return state.collections.find(collection => collection.slug === state.active) || null;
}

function statusCounts() {
  return state.images.reduce((counts, asset) => {
    const key = asset.status || 'unreviewed';
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {approved: 0, maybe: 0, rejected: 0, unreviewed: 0});
}

function statusDisplay(status) {
  return ({approved: '已通过', maybe: '待定', rejected: '已拒绝', '': '待人工审核'})[status || ''] || status;
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

function assetTypeLabel(asset) {
  const name = asset?.name || asset?.rel || '';
  const match = name.match(/\.([a-z0-9]+)$/i);
  return match ? match[1].toUpperCase() : '';
}

function scopeTitle() {
  if (state.scope.kind === 'all') return '所有项目';
  if (state.scope.kind === 'project') return state.scope.project || '项目';
  if (state.scope.kind === 'scene') return [state.scope.project, state.scope.scene].filter(Boolean).join(' · ') || '场景';
  return activeCollectionRecord()?.label || 'Asset Viewer';
}

function scopeCaption() {
  if (state.scope.kind === 'all') return '跨项目素材浏览';
  if (state.scope.kind === 'project') return '项目全部场景';
  if (state.scope.kind === 'scene') return '场景全部素材';
  const taxonomy = collectionTaxonomy(state.active);
  return [taxonomy.project, taxonomy.scene, useLabel(taxonomy.use)].filter(Boolean).join(' · ');
}

function renderWorkspaceState() {
  const collection = activeCollectionRecord();
  const counts = statusCounts();
  const title = scopeTitle();
  $('#collectionTitle').textContent = title;
  $('#scopeLabel').textContent = scopeCaption();
  document.title = collection && isSingleCollectionScope() ? `${collection.label} · Asset Viewer` : 'Asset Viewer';

  const parts = [`${state.images.length} 项`, `${counts.approved} 已通过`, `${counts.rejected} 已拒绝`, `${counts.unreviewed} 待人工审核`];
  if (counts.maybe) parts.splice(2, 0, `${counts.maybe} 待定`);
  if (state.scan?.truncated) parts.push(`扫描未完成（${state.scan.reason || '限制'}）`);
  $('#summary').textContent = parts.join(' · ');

  document.querySelectorAll('[data-workspace-tab]').forEach(button => {
    const selected = button.dataset.workspaceTab === state.workspaceTab;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-current', selected ? 'page' : 'false');
  });
  const workspace = document.querySelector('.workspace');
  workspace?.classList.toggle('activity-mode', state.workspaceTab === 'activity');
  workspace?.classList.toggle('approved-mode', state.workspaceTab === 'approved');

  const approved = state.images.filter(asset => asset.status === 'approved');
  const approvedMode = state.workspaceTab === 'approved' && state.view === 'gallery';
  const singleCollection = isSingleCollectionScope();
  document.querySelector('.content-frame')?.classList.toggle('approved-mode', approvedMode);
  $('#approvedPanel').classList.toggle('hidden', !approvedMode);
  $('#approvedCount').textContent = `${approved.length} 项已通过素材`;
  $('#approvedSummaryText').textContent = approved.length
    ? (singleCollection ? '可使用原有交接清单，不会移动或改写原文件。' : '当前为跨集合视图；请进入单个集合后导出交接清单。')
    : '人工通过素材后，可在单个集合中交接。';
  $('#approvedHandoff').disabled = approved.length === 0 || !singleCollection;
  $('#approvedReport').disabled = state.images.length === 0 || !singleCollection;
  const totalBytes = approved.reduce((total, asset) => total + Number(asset.size || 0), 0);
  $('#approvedMeta').innerHTML = `<div><span>范围</span><strong>${esc(title)}</strong></div><div><span>已通过</span><strong>${approved.length}</strong></div>${totalBytes ? `<div><span>合计大小</span><strong>${esc(formatBytes(totalBytes))}</strong></div>` : ''}`;

  if (state.workspaceTab === 'activity') $('#workspaceCount').textContent = singleCollection ? `${state.activityEvents.length} 条记录` : '按集合查看历史';
  else if (approvedMode) $('#workspaceCount').textContent = `${approved.length} 已通过`;
  else $('#workspaceCount').textContent = `${state.filtered.length} 项显示中`;
}

function scopedCollections(project = '', scene = '') {
  return state.collections.filter(collection => {
    const taxonomy = collectionTaxonomy(collection);
    return (!project || taxonomy.project === project) && (!scene || taxonomy.scene === scene);
  });
}

function renderFacetControls() {
  const projectSelect = $('#projectFilter');
  const sceneSelect = $('#sceneFilter');
  const useSelect = $('#useFilter');
  const activeTaxonomy = collectionTaxonomy(state.active);
  const projects = [...new Set(state.collections.map(collection => collectionTaxonomy(collection).project))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
  const selectedProject = state.scope.kind === 'all' ? '__all__' : (['project', 'scene'].includes(state.scope.kind) ? state.scope.project : '');
  const projectOptions = [
    '<option value="">当前集合</option>',
    '<option value="__all__">所有项目</option>',
    ...projects.map(project => `<option value="${esc(project)}">${esc(project)}</option>`),
  ];
  projectSelect.innerHTML = projectOptions.join('');
  projectSelect.value = selectedProject || '';

  const sceneProject = state.scope.kind === 'all' ? '' : (selectedProject || activeTaxonomy.project);
  const scenes = [...new Set(scopedCollections(sceneProject).map(collection => collectionTaxonomy(collection).scene))]
    .sort((a, b) => a.localeCompare(b, 'zh-CN'));
  sceneSelect.innerHTML = `<option value="">${state.scope.kind === 'all' ? '全部项目的场景' : (sceneProject ? '全部场景' : '当前集合')}</option>` + scenes.map(scene => `<option value="${esc(scene)}">${esc(scene)}</option>`).join('');
  sceneSelect.disabled = state.scope.kind === 'all' || !sceneProject || scenes.length === 0;
  sceneSelect.value = state.scope.kind === 'scene' ? state.scope.scene : '';

  const previousUse = useSelect.value || 'all';
  useSelect.innerHTML = '<option value="all">全部用途</option>' + Object.entries(USE_LABELS)
    .map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join('');
  useSelect.value = USE_LABELS[previousUse] ? previousUse : 'all';
}

function renderCollections(active) {
  const select = $('#collection');
  const nav = $('#collectionNav');
  const projects = new Map();
  for (const collection of state.collections) {
    const taxonomy = collectionTaxonomy(collection);
    if (!projects.has(taxonomy.project)) projects.set(taxonomy.project, new Map());
    const scenes = projects.get(taxonomy.project);
    if (!scenes.has(taxonomy.scene)) scenes.set(taxonomy.scene, []);
    scenes.get(taxonomy.scene).push(collection);
  }

  const parts = [];
  const navParts = [`<button type="button" class="scope-link${state.scope.kind === 'all' ? ' active' : ''}" data-all-projects><span class="scope-dot" aria-hidden="true"></span><span>所有项目</span><small>${state.collections.length}</small></button>`];
  for (const [project, scenes] of [...projects].sort(([a], [b]) => a.localeCompare(b, 'zh-CN'))) {
    const rows = [...scenes.values()].flat();
    const isProjectActive = (state.scope.kind === 'project' || state.scope.kind === 'scene') && state.scope.project === project;
    navParts.push(`<section class="project-node"><button type="button" class="project-link${isProjectActive ? ' active' : ''}" data-project="${esc(project)}"><span>${esc(project)}</span><small>${rows.length}</small></button><div class="scene-list">`);
    const optionRows = [];
    for (const [scene, collections] of [...scenes].sort(([a], [b]) => a.localeCompare(b, 'zh-CN'))) {
      const isSceneActive = state.scope.kind === 'scene' && state.scope.project === project && state.scope.scene === scene;
      navParts.push(`<button type="button" class="scene-link${isSceneActive ? ' active' : ''}" data-project="${esc(project)}" data-scene="${esc(scene)}"><span>${esc(scene)}</span><small>${collections.length}</small></button><div class="collection-list">`);
      for (const collection of collections) {
        const taxonomy = collectionTaxonomy(collection);
        navParts.push(`<button type="button" class="collection-link${state.scope.kind === 'collection' && collection.slug === active ? ' active' : ''}" data-collection="${esc(collection.slug)}"${collection.available === false ? ' disabled' : ''}><span class="collection-icon" aria-hidden="true"></span><span>${esc(collection.label)}</span><em>${esc(useLabel(taxonomy.use))}</em>${collection.available === false ? '<small>不可用</small>' : ''}</button>`);
        optionRows.push(`<option value="${esc(collection.slug)}">${esc(collection.label)}${collection.available === false ? ' — 不可用' : ''}</option>`);
      }
      navParts.push('</div>');
    }
    navParts.push('</div></section>');
    parts.push(`<optgroup label="${esc(project)}">${optionRows.join('')}</optgroup>`);
  }
  select.innerHTML = parts.join('');
  nav.innerHTML = navParts.join('');
  if (active && state.collections.some(collection => collection.slug === active)) select.value = active;
  nav.querySelectorAll('[data-collection]').forEach(button => {
    button.onclick = () => switchCollection(button.dataset.collection).catch(showError);
  });
  nav.querySelectorAll('[data-project]').forEach(button => {
    button.onclick = () => setProjectScope(button.dataset.project).catch(showError);
  });
  nav.querySelectorAll('[data-scene]').forEach(button => {
    button.onclick = () => setSceneScope(button.dataset.project, button.dataset.scene).catch(showError);
  });
  nav.querySelector('[data-all-projects]')?.addEventListener('click', () => setAllProjectsScope().catch(showError));
}

async function setProjectScope(project) {
  if (!project) return load(state.active);
  const collections = scopedCollections(project);
  return loadScope(collections.map(collection => collection.slug), {kind: 'project', project, scene: ''});
}

async function setSceneScope(project, scene) {
  if (!project || !scene) return setProjectScope(project);
  const collections = scopedCollections(project, scene);
  return loadScope(collections.map(collection => collection.slug), {kind: 'scene', project, scene});
}

async function setAllProjectsScope() {
  return loadScope(state.collections.map(collection => collection.slug), {kind: 'all', project: '', scene: ''});
}

function provenanceText(asset) {
  const p = asset.provenance || {};
  const extra = p.extra && typeof p.extra === 'object' ? Object.values(p.extra) : [];
  const taxonomy = assetTaxonomy(asset);
  return [p.source_project, p.tool, p.agent, p.model, p.prompt, p.seed, p.run_id, p.git_commit, taxonomy.project, taxonomy.scene, taxonomy.topic, useLabel(taxonomy.use), ...extra].filter(Boolean).join(' ').toLowerCase();
}

function assetSearchMatch(asset, query) {
  if (!query) return true;
  const terms = query.match(/(?:[^\s"]+|"[^"]*")+/g) || [];
  const p = asset.provenance || {};
  const taxonomy = assetTaxonomy(asset);
  const fields = {
    status: asset.status || 'unreviewed', family: asset.family_name || '', project: taxonomy.project || p.source_project || '',
    scene: taxonomy.scene || '', use: useLabel(taxonomy.use), purpose: useLabel(taxonomy.use),
    tool: p.tool || '', agent: p.agent || '', model: p.model || '', prompt: p.prompt || '', run: p.run_id || '',
  };
  const haystack = `${asset.name} ${asset.rel} ${asset.comment || ''} ${asset.family_name || ''} ${provenanceText(asset)}`.toLowerCase();
  return terms.every(raw => {
    const term = raw.replace(/^"|"$/g, '');
    const split = term.indexOf(':');
    if (split > 0) {
      const key = term.slice(0, split).toLowerCase();
      const value = term.slice(split + 1).replace(/^"|"$/g, '').toLowerCase();
      if (key in fields) return String(fields[key]).toLowerCase().includes(value);
    }
    return haystack.includes(term.toLowerCase());
  });
}

function isQuickReviewOpen() {
  const panel = $('#quickReview');
  return Boolean(state.quickReview.open && panel && !panel.classList.contains('hidden'));
}

function quickReviewAssetAt(index = state.quickReview.index) {
  const key = state.quickReview.keys[index];
  return key ? state.images.find(asset => keyFor(asset) === key) || null : null;
}

function quickReviewAssets() {
  return state.quickReview.keys.map(key => state.images.find(asset => keyFor(asset) === key)).filter(Boolean);
}

function quickReviewCandidateAssets() {
  const visible = state.filtered.filter(asset => asset.present !== false);
  // A mixed project view should begin with candidate design material. Other
  // material remains available when a reviewer deliberately filters to it.
  const designs = visible.filter(asset => assetTaxonomy(asset).use === 'design');
  const candidates = designs.length ? designs : visible;
  return [...candidates.filter(asset => !asset.status), ...candidates.filter(asset => asset.status)];
}

function updateQuickReviewLaunch() {
  const button = $('#quickReviewLaunch');
  if (!button) return;
  const candidates = quickReviewCandidateAssets();
  const designs = candidates.filter(asset => assetTaxonomy(asset).use === 'design');
  button.disabled = state.workspaceTab !== 'gallery' || candidates.length === 0;
  button.textContent = '快速审核';
  button.title = designs.length
    ? `优先从 ${candidates.filter(asset => !asset.status).length} 项待人工审核的候选设计开始。`
    : '按当前筛选结果逐张审看；不会自动改变任何审核结果。';
}

function clearQuickReviewCommentTimer() {
  if (state.quickReview.commentTimer) clearTimeout(state.quickReview.commentTimer);
  state.quickReview.commentTimer = null;
}

function resetQuickReview() {
  clearQuickReviewCommentTimer();
  state.quickReviewRun += 1;
  state.quickReviewSaveRequest += 1;
  state.quickReviewCommentRequest += 1;
  state.quickReview.open = false;
  state.quickReview.runId = 0;
  state.quickReview.keys = [];
  state.quickReview.index = 0;
  state.quickReview.saving = false;
  state.quickReview.lastAction = null;
  state.quickReview.commentPromise = null;
  const panel = $('#quickReview');
  panel?.classList.add('hidden');
  panel?.classList.remove('suspended');
  panel?.setAttribute('aria-hidden', 'true');
  const video = $('#quickReviewVideo');
  if (video) {
    video.pause();
    video.removeAttribute('src');
    video.removeAttribute('poster');
    video.load();
  }
}

function closeQuickReview({refresh = true} = {}) {
  if (!state.quickReview.open) return;
  resetQuickReview();
  if (refresh) applyFilter();
  if (modal.classList.contains('hidden') && compareModal.classList.contains('hidden')) document.body.style.overflow = '';
}

function startQuickReview() {
  const candidates = quickReviewCandidateAssets();
  if (!candidates.length) return;
  if (state.selectionMode) setSelectionMode(false, false);
  const quick = state.quickReview;
  quick.open = true;
  quick.keys = candidates.map(keyFor);
  quick.index = 0;
  quick.scopeRequest = state.scopeRequest;
  quick.runId = ++state.quickReviewRun;
  quick.saving = false;
  quick.lastAction = null;
  const panel = $('#quickReview');
  panel.classList.remove('hidden');
  panel.classList.remove('suspended');
  panel.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  renderQuickReview();
  requestAnimationFrame(() => $('#quickReviewApprove')?.focus());
}

function quickReviewMove(amount, force = false) {
  const quick = state.quickReview;
  if (!quick.open || (quick.saving && !force)) return;
  quick.index = Math.max(0, Math.min(quick.keys.length - 1, quick.index + amount));
  renderQuickReview();
}

function updateQuickReviewAsset(assetKey, patch) {
  const asset = state.images.find(candidate => keyFor(candidate) === assetKey);
  if (asset) Object.assign(asset, patch);
  return asset;
}

function quickReviewSessionIsCurrent(runId, assetKey = '') {
  return isQuickReviewOpen()
    && state.quickReview.runId === runId
    && state.quickReview.scopeRequest === state.scopeRequest
    && (!assetKey || keyFor(quickReviewAssetAt()) === assetKey);
}

function quickReviewRequestIsCurrent(request, assetKey, runId) {
  return isQuickReviewOpen()
    && request === state.quickReviewSaveRequest
    && quickReviewSessionIsCurrent(runId, assetKey);
}

function setQuickReviewMedia(asset) {
  const image = $('#quickReviewImage');
  const video = $('#quickReviewVideo');
  const isVideo = asset.media_type === 'video';
  image.classList.toggle('hidden', isVideo);
  video.classList.toggle('hidden', !isVideo);
  if (isVideo) {
    image.removeAttribute('src');
    image.alt = '';
    video.poster = asset.preview || asset.thumb;
    if (video.getAttribute('src') !== asset.file) {
      video.src = asset.file;
      video.load();
    }
    video.setAttribute('aria-label', asset.name);
  } else {
    video.pause();
    video.removeAttribute('src');
    video.removeAttribute('poster');
    image.src = assetSrc(asset);
    image.alt = asset.name;
  }
}

function renderQuickReview() {
  if (!isQuickReviewOpen()) return;
  const quick = state.quickReview;
  const assets = quickReviewAssets();
  if (assets.length !== quick.keys.length) quick.keys = assets.map(keyFor);
  if (!assets.length) {
    closeQuickReview();
    return;
  }
  quick.index = Math.max(0, Math.min(quick.index, assets.length - 1));
  const asset = assets[quick.index];
  setQuickReviewMedia(asset);
  $('#quickReviewTitle').textContent = asset.name;
  const resolution = asset.width && asset.height ? `${asset.width} × ${asset.height}` : assetTypeLabel(asset);
  $('#quickReviewDetails').textContent = [asset.rel, resolution, formatBytes(asset.size)].filter(Boolean).join(' · ');
  const status = asset.status || '';
  const statusElement = $('#quickReviewStatus');
  statusElement.textContent = statusDisplay(status);
  statusElement.className = `quick-review-status ${status ? `status-${status}` : 'status-unreviewed'}`;
  const reviewed = assets.filter(candidate => candidate.status).length;
  $('#quickReviewProgress').textContent = `${quick.index + 1} / ${assets.length} · ${reviewed} 已评价`;
  $('#quickReviewProgressBar').style.width = `${((quick.index + 1) / assets.length) * 100}%`;
  const controlsDisabled = quick.saving;
  $('#quickReviewPrevious').disabled = controlsDisabled || quick.index === 0;
  $('#quickReviewNext').disabled = controlsDisabled || quick.index >= assets.length - 1;
  $('#quickReviewApprove').disabled = controlsDisabled;
  $('#quickReviewMaybe').disabled = controlsDisabled;
  $('#quickReviewReject').disabled = controlsDisabled;
  $('#quickReviewUndo').disabled = controlsDisabled || !quick.lastAction;
  $('#quickReviewCompare').disabled = controlsDisabled || quick.index === 0;
  for (const [buttonId, buttonStatus] of [['quickReviewApprove', 'approved'], ['quickReviewMaybe', 'maybe'], ['quickReviewReject', 'rejected']]) {
    const button = $(`#${buttonId}`);
    button.classList.toggle('decision-active', status === buttonStatus);
    button.setAttribute('aria-pressed', String(status === buttonStatus));
  }
  const comment = $('#quickReviewComment');
  if (document.activeElement !== comment) comment.value = asset.comment || '';
  const strip = $('#quickReviewFilmstrip');
  strip.innerHTML = assets.map((candidate, index) => `<button type="button" class="quick-review-frame${index === quick.index ? ' active' : ''}${candidate.status ? ` ${esc(candidate.status)}` : ''}" data-quick-index="${index}" aria-current="${index === quick.index ? 'true' : 'false'}" aria-label="${esc(`${index + 1}. ${candidate.name} · ${statusDisplay(candidate.status)}`)}"><img src="${esc(candidate.thumb)}" alt=""><span>${candidate.media_type === 'video' ? '▶' : index + 1}</span></button>`).join('');
  strip.querySelectorAll('[data-quick-index]').forEach(button => {
    button.onclick = () => {
      if (state.quickReview.saving) return;
      state.quickReview.index = Number(button.dataset.quickIndex);
      renderQuickReview();
    };
  });
  requestAnimationFrame(() => strip.querySelector('.quick-review-frame.active')?.scrollIntoView({block: 'nearest', inline: 'center'}));
}

function enqueueQuickReviewCommentSave(assetKey, collection, assetId, comment, runId) {
  const quick = state.quickReview;
  const previous = quick.commentPromise || Promise.resolve();
  const promise = previous.catch(() => null).then(() =>
    saveQuickReviewCommentSnapshot(assetKey, collection, assetId, comment, runId)
  );
  quick.commentPromise = promise;
  promise.finally(() => {
    if (state.quickReview.commentPromise === promise) state.quickReview.commentPromise = null;
  }).catch(() => null);
  return promise;
}

async function flushQuickReviewComment(asset, comment, runId) {
  clearQuickReviewCommentTimer();
  const quick = state.quickReview;
  if (quick.commentPromise) await quick.commentPromise.catch(() => null);
  if (!quickReviewSessionIsCurrent(runId, keyFor(asset))) return false;
  if (comment === (asset.comment || '')) return true;
  const promise = enqueueQuickReviewCommentSave(keyFor(asset), asset.collection, asset.asset_id, comment, runId);
  const saved = await promise.then(() => true).catch(error => {
    showError(error);
    return false;
  });
  return saved && quickReviewSessionIsCurrent(runId, keyFor(asset));
}

async function saveQuickReviewCommentSnapshot(assetKey, collection, assetId, comment, runId) {
  const request = ++state.quickReviewCommentRequest;
  const current = quickReviewAssetAt();
  if (quickReviewSessionIsCurrent(runId, assetKey) && current && keyFor(current) === assetKey) $('#quickReviewCommentState').textContent = '保存中…';
  await apiPost('/api/comment', {collection, asset_id: assetId, comment});
  updateQuickReviewAsset(assetKey, {comment, is_new: false});
  renderWorkspaceState();
  if (quickReviewSessionIsCurrent(runId, assetKey) && request === state.quickReviewCommentRequest && $('#quickReviewComment').value === comment) {
    $('#quickReviewCommentState').textContent = '已保存';
  }
}

function scheduleQuickReviewCommentSave() {
  clearQuickReviewCommentTimer();
  const asset = quickReviewAssetAt();
  if (!asset) return;
  const assetKey = keyFor(asset);
  const collection = asset.collection;
  const assetId = asset.asset_id;
  const comment = $('#quickReviewComment').value;
  const runId = state.quickReview.runId;
  $('#quickReviewCommentState').textContent = '未保存';
  state.quickReview.commentTimer = setTimeout(() => {
    state.quickReview.commentTimer = null;
    enqueueQuickReviewCommentSave(assetKey, collection, assetId, comment, runId).catch(showError);
  }, 500);
}

async function quickReviewDecision(status) {
  const quick = state.quickReview;
  if (!isQuickReviewOpen() || quick.saving) return;
  const asset = quickReviewAssetAt();
  if (!asset) return;
  const assetKey = keyFor(asset);
  const runId = quick.runId;
  const decisionComment = $('#quickReviewComment').value;
  const unchanged = (asset.status || '') === status;
  quick.saving = true;
  renderQuickReview();
  let request = 0;
  try {
    if (!await flushQuickReviewComment(asset, decisionComment, runId)) return;
    if (!quickReviewSessionIsCurrent(runId, assetKey)) return;
    if (unchanged) {
      quickReviewMove(1, true);
      return;
    }
    const previousStatus = asset.status || '';
    const previousComment = asset.comment || '';
    request = ++state.quickReviewSaveRequest;
    await apiPost('/api/review', {collection: asset.collection, asset_id: asset.asset_id, status, comment: decisionComment});
    updateQuickReviewAsset(assetKey, {status, comment: decisionComment, is_new: false});
    renderWorkspaceState();
    if (!quickReviewRequestIsCurrent(request, assetKey, runId)) return;
    quick.lastAction = {assetKey, collection: asset.collection, assetId: asset.asset_id, index: quick.index, status, comment: decisionComment, previousStatus, previousComment};
    quickReviewMove(1, true);
    refreshReviewState().catch(showError);
  } finally {
    if (quickReviewSessionIsCurrent(runId) && (!request || request === state.quickReviewSaveRequest)) {
      quick.saving = false;
      renderQuickReview();
    }
  }
}

async function quickReviewUndo() {
  const quick = state.quickReview;
  const action = quick.lastAction;
  if (!isQuickReviewOpen() || quick.saving || !action) return;
  const runId = quick.runId;
  const request = ++state.quickReviewSaveRequest;
  quick.saving = true;
  renderQuickReview();
  try {
    const params = new URLSearchParams({collection: action.collection, asset_id: action.assetId});
    const historyResponse = await fetch('/api/review-history?' + params, {cache: 'no-store'});
    if (!historyResponse.ok) throw new Error('无法确认可撤销的审核记录');
    const history = await historyResponse.json();
    if (!quickReviewSessionIsCurrent(runId) || request !== state.quickReviewSaveRequest) return;
    const latest = (history.events || []).find(event => !event.undone_at);
    if (!latest || latest.action !== 'review' || latest.old_status !== action.previousStatus || latest.new_status !== action.status || latest.old_comment !== action.previousComment || latest.new_comment !== action.comment) {
      throw new Error('该素材已有后续意见或审核变更；请在详情的历史记录中处理，避免误撤销。');
    }
    const data = await apiPost('/api/undo', {collection: action.collection, asset_id: action.assetId});
    const result = data.result || {};
    updateQuickReviewAsset(action.assetKey, {status: result.status || '', comment: result.comment ?? action.previousComment});
    renderWorkspaceState();
    if (!quickReviewSessionIsCurrent(runId) || request !== state.quickReviewSaveRequest) return;
    const index = quick.keys.indexOf(action.assetKey);
    if (index >= 0) quick.index = index;
    quick.lastAction = null;
    refreshReviewState().catch(showError);
  } finally {
    if (quickReviewSessionIsCurrent(runId) && request === state.quickReviewSaveRequest) {
      quick.saving = false;
      renderQuickReview();
    }
  }
}

function openQuickReviewDetails() {
  const asset = quickReviewAssetAt();
  if (!asset) return;
  const index = state.filtered.findIndex(candidate => keyFor(candidate) === keyFor(asset));
  if (index < 0) {
    showError(new Error('该素材已不在当前筛选范围内；退出快速审核后可在图库中打开详情。'));
    return;
  }
  $('#quickReview').classList.add('suspended');
  $('#quickReview').setAttribute('aria-hidden', 'true');
  openAt(index);
}

function compareQuickReviewAssets() {
  const assets = quickReviewAssets();
  const index = state.quickReview.index;
  if (index < 1 || !assets[index]) return;
  $('#quickReview').classList.add('suspended');
  $('#quickReview').setAttribute('aria-hidden', 'true');
  openCompareAssets([assets[index - 1], assets[index]]);
}

function applyFilter() {
  const filter = $('#filter').value;
  const use = $('#useFilter').value;
  const query = $('#search').value.trim().toLowerCase();
  const sort = $('#sort').value;
  let rows = state.images.filter(asset => {
    const statusMatch = filter === 'all' ||
      (filter === 'new' ? asset.is_new : (filter === 'unreviewed' ? !asset.status : asset.status === filter));
    const useMatch = use === 'all' || assetTaxonomy(asset).use === use;
    return statusMatch && useMatch && assetSearchMatch(asset, query);
  });
  const rank = {'': 0, maybe: 1, approved: 2, rejected: 3};
  rows = [...rows].sort((a, b) => {
    if (sort === 'oldest') return a.mtime - b.mtime;
    if (sort === 'name') return a.rel.localeCompare(b.rel);
    if (sort === 'status') return (rank[a.status] - rank[b.status]) || a.rel.localeCompare(b.rel);
    if (sort === 'size') return b.size - a.size || a.rel.localeCompare(b.rel);
    if (sort === 'resolution') return ((b.width || 0) * (b.height || 0)) - ((a.width || 0) * (a.height || 0));
    return b.mtime - a.mtime;
  });
  state.filtered = rows;
  grid.innerHTML = rows.map((asset, index) => {
    const selected = state.selected.has(keyFor(asset));
    const taxonomy = assetTaxonomy(asset);
    const statusClass = asset.status || 'needs-review';
    const technical = [
      assetTypeLabel(asset),
      asset.width && asset.height ? `${asset.width} × ${asset.height}` : '',
      formatBytes(asset.size),
    ].filter(Boolean).map(value => `<span class="tech-chip">${esc(value)}</span>`).join('');
    const path = asset.rel && asset.rel !== asset.name ? `<div class="path" title="${esc(asset.rel)}">${esc(asset.rel)}</div>` : '';
    const note = taxonomy.note ? `<span class="context-note">${esc(taxonomy.note)}</span>` : '';
    return `<article class="card ${selected ? 'selected' : ''}${state.selectionMode ? ' selection-ready' : ''}" data-i="${index}" tabindex="0" aria-pressed="${state.selectionMode ? String(selected) : 'false'}">
      <div class="thumb-wrap"><img class="thumb" loading="lazy" src="${esc(asset.thumb)}" alt="${esc(asset.name)}">${asset.media_type === 'video' ? '<span class="video-play" aria-hidden="true">▶</span>' : ''}<span class="badge ${esc(statusClass)}">${esc(statusDisplay(asset.status))}</span><input class="select-toggle" data-select="${index}" type="checkbox" ${selected ? 'checked' : ''} ${state.selectionMode ? '' : 'tabindex="-1"'} aria-label="${selected ? '取消选择' : '选择'} ${esc(asset.name)}"></div>
      <div class="caption">
        <div class="name">${esc(asset.name)}</div>
        <div class="content-chips"><span class="scene-chip">${esc(taxonomy.scene)}</span><span class="use-chip use-${esc(taxonomy.use)}">${esc(useLabel(taxonomy.use))}</span>${note}</div>
        ${path}
        <div class="tech-row">${technical}</div>
        <div class="meta-row">${asset.is_new ? '<span class="new-dot">新素材</span>' : ''}${asset.comment ? '<span class="note-dot">有批注</span>' : ''}${asset.annotation_count ? `<span class="note-dot">${asset.annotation_count} 个标注</span>` : ''}${asset.family_name ? `<span class="family-chip">${esc(asset.family_name)}</span>` : ''}${asset.family_preferred ? '<span class="preferred-chip">偏好方案</span>' : ''}</div>
      </div>
    </article>`;
  }).join('');
  $('#empty').classList.toggle('hidden', rows.length > 0);
  grid.querySelectorAll('.card').forEach(element => {
    const index = Number(element.dataset.i);
    element.onclick = event => {
      if (event.target.closest('[data-select]')) return;
      if (state.selectionMode || event.ctrlKey || event.metaKey || event.shiftKey) {
        if (!state.selectionMode) setSelectionMode(true, false);
        if (event.shiftKey) selectRange(index); else toggleSelection(index);
        return;
      }
      openAt(index);
    };
    element.onkeydown = event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      if (state.selectionMode) {
        if (event.shiftKey) selectRange(index); else toggleSelection(index);
      } else {
        openAt(index);
      }
    };
  });
  grid.querySelectorAll('[data-select]').forEach(button => {
    button.onclick = event => {
      event.preventDefault();
      event.stopPropagation();
      if (!state.selectionMode) setSelectionMode(true, false);
      toggleSelection(Number(button.dataset.select));
    };
  });
  renderWorkspaceState();
  updateQuickReviewLaunch();
  updateBulk();
}

function setMenu(select, placeholder, items) {
  select.innerHTML = `<option value="">${esc(placeholder)}</option>` + items.map(item =>
    `<option value="${esc(item.value)}"${item.disabled ? ' disabled' : ''}>${esc(item.label)}</option>`
  ).join('');
  select.value = '';
}

function setSelectionMode(enabled, render = true) {
  state.selectionMode = Boolean(enabled);
  if (!state.selectionMode) {
    state.selected.clear();
    state.selectionAnchor = null;
    state.selectionAnchorKey = '';
  }
  document.body.classList.toggle('selection-mode', state.selectionMode);
  const toggle = $('#selectionMode');
  toggle.classList.toggle('active', state.selectionMode);
  toggle.setAttribute('aria-pressed', String(state.selectionMode));
  toggle.textContent = state.selectionMode ? '完成选择' : '多选';
  if (render) applyFilter();
}

function selectAllVisible() {
  if (!state.selectionMode) setSelectionMode(true, false);
  state.filtered.forEach(asset => state.selected.add(keyFor(asset)));
  state.selectionAnchor = state.filtered.length ? 0 : null;
  state.selectionAnchorKey = state.filtered.length ? keyFor(state.filtered[0]) : '';
  applyFilter();
}

function toggleSelection(index) {
  const asset = state.filtered[index];
  if (!asset) return;
  const key = keyFor(asset);
  state.selected.has(key) ? state.selected.delete(key) : state.selected.add(key);
  state.selectionAnchor = index;
  state.selectionAnchorKey = key;
  applyFilter();
}

function selectRange(index) {
  const asset = state.filtered[index];
  if (!asset) return;
  const anchorIndex = state.selectionAnchorKey
    ? state.filtered.findIndex(candidate => keyFor(candidate) === state.selectionAnchorKey)
    : -1;
  const anchor = anchorIndex >= 0 ? anchorIndex : index;
  const start = Math.min(anchor, index);
  const end = Math.max(anchor, index);
  for (let position = start; position <= end; position += 1) state.selected.add(keyFor(state.filtered[position]));
  state.selectionAnchor = index;
  state.selectionAnchorKey = keyFor(asset);
  applyFilter();
}

function updateCollectionActions() {
  const review = state.reviewState;
  const singleCollection = isSingleCollectionScope();
  const items = [
    {value: 'select-all', label: `全选当前结果（${state.filtered.length}）`, disabled: !state.filtered.length},
    {value: 'save-view', label: '保存当前视图…'},
    ...(state.activeSavedView ? [{value: 'delete-view', label: '删除当前保存视图'}] : []),
    {value: 'report', label: '打开审核报告', disabled: !singleCollection},
    {value: 'handoff', label: '导出已通过交接清单', disabled: !singleCollection},
    {value: 'refresh', label: '刷新当前范围'},
  ];
  if (singleCollection && review && !review.scan_incomplete && !review.empty) {
    if (review.complete) items.splice(1, 0, {value: 'reopen', label: '重新打开审核'});
    else if (!review.unreviewed) items.splice(1, 0, {value: 'complete', label: review.stale ? '重新完成审核' : '标记审核完成'});
  }
  setMenu($('#actions'), '更多操作…', items);
}

function updateBulk() {
  const assets = allSelectedAssets();
  const count = assets.length;
  const selectedCollections = new Set(assets.map(asset => asset.collection));
  const singleCollection = selectedCollections.size <= 1;
  $('#bulkbar').classList.toggle('hidden', !(state.selectionMode || count > 0));
  $('#selectedCount').textContent = count ? `${count} 项已选择` : '选择素材';
  $('#selectionScopeNote').textContent = !count
    ? '点击卡片连续选择，或全选当前结果。'
    : (singleCollection
      ? '批量审核会保留原生审核历史。'
      : '跨集合选择仅用于比较；审核和版本关系请在单个集合中完成。');
  $('#selectAllVisible').disabled = state.filtered.length === 0;
  $('#clearSelection').disabled = count === 0;
  const items = [
    {value: 'approve', label: '设为已通过', disabled: !count || !singleCollection}, {value: 'maybe', label: '设为待定', disabled: !count || !singleCollection},
    {value: 'reject', label: '设为已拒绝', disabled: !count || !singleCollection}, {value: 'clear-review', label: '清除审核状态', disabled: !count || !singleCollection},
    {value: 'compare', label: '比较选中方案', disabled: count < 2},
    {value: 'group', label: '建立版本组', disabled: count < 2 || !singleCollection},
    {value: 'clear-selection', label: '清空选择'},
  ];
  setMenu($('#selectionAction'), '批量操作…', items);
  updateCollectionActions();
}

function updateReviewState() {
  updateCollectionActions();
}

async function refreshReviewState() {
  if (!state.active || !isSingleCollectionScope()) {
    state.reviewState = null;
    updateReviewState();
    return;
  }
  const active = state.active;
  const scopeRequest = state.scopeRequest;
  const response = await fetch('/api/pending?collection=' + encodeURIComponent(active), {cache: 'no-store'});
  if (!response.ok) return;
  const data = await response.json();
  if (scopeRequest !== state.scopeRequest || !isSingleCollectionScope() || state.active !== active) return;
  state.reviewState = data.collections[0] || null;
  updateReviewState();
}

function familyById(familyId, collection = '') {
  return state.families.find(family => family.family_id === familyId && (!collection || family.collection === collection)) || null;
}

function renderReviewDecision(asset) {
  const status = asset?.status || '';
  const labels = {approved: '已通过', maybe: '待定', rejected: '已拒绝', '': '待人工审核'};
  const indicator = $('#reviewDecision');
  indicator.textContent = labels[status] || status;
  indicator.className = `review-decision ${status ? `decision-${status}` : 'decision-unreviewed'}`;
  $('#reviewStatus').value = status;
}

function metadataValue(value) {
  if (Array.isArray(value)) return value.map(metadataValue).join(' · ');
  if (value && typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return String(value); }
  }
  return String(value ?? '');
}

function renderProvenance(asset) {
  const p = asset?.provenance || {};
  const taxonomy = assetTaxonomy(asset);
  const rows = [
    ['浏览分类', [taxonomy.project, taxonomy.scene, useLabel(taxonomy.use)].filter(Boolean).join(' · ')],
    ['来源项目', p.source_project], ['工具', p.tool], ['代理', p.agent], ['模型', p.model],
    ['提示词', p.prompt], ['随机种子', p.seed], ['运行批次', p.run_id], ['Git 提交', p.git_commit],
    ['父级素材', p.parent_asset_id],
  ].filter(([, value]) => value);
  const extras = p.extra && typeof p.extra === 'object' ? Object.entries(p.extra) : [];
  const content = $('#provenanceContent');
  if (rows.length === 1 && !extras.length) {
    content.innerHTML = `${rows.map(([key, value]) => `<div class="provenance-row"><strong>${esc(key)}</strong><span>${esc(metadataValue(value))}</span></div>`).join('')}<span class="muted">来源：未声明／未导入</span>`;
    return;
  }
  content.innerHTML = [...rows, ...extras].map(([key, value]) =>
    `<div class="provenance-row"><strong>${esc(key)}</strong><span>${esc(metadataValue(value))}</span></div>`
  ).join('');
}

function updateAssetActions(asset) {
  const items = [
    {value: 'history', label: '审核历史'},
    {value: 'undo', label: '撤销上次审核变更'},
    {value: 'copy-path', label: '复制相对路径'},
    {value: 'copy-link', label: '复制审核链接'},
    {value: 'open-original', label: '打开原文件'},
  ];
  setMenu($('#assetActions'), '更多操作…', items);
}

function renderFamilyRow(asset) {
  const row = $('#familyRow');
  if (!asset || !asset.family_id) {
    row.classList.add('hidden');
    setMenu($('#familyActions'), '版本组…', []);
    return;
  }
  const family = familyById(asset.family_id, asset.collection);
  const count = family ? family.members.filter(member => member.present).length : state.images.filter(item => item.family_id === asset.family_id).length;
  const latest = family && family.latest_asset_id === asset.asset_id;
  $('#familyName').textContent = asset.family_name || (family && family.name) || '版本组';
  $('#familyMeta').textContent = `${count} 个版本${asset.family_preferred ? ' · 偏好方案' : ''}${latest ? ' · 最新' : ''}`;
  setMenu($('#familyActions'), '版本组…', [
    {value: 'compare', label: '比较版本组', disabled: count < 2},
    {value: 'prefer', label: asset.family_preferred ? '已设为偏好方案' : '设为偏好方案', disabled: Boolean(asset.family_preferred)},
    {value: 'leave', label: '移出版本组'},
  ]);
  row.classList.remove('hidden');
}

function openAt(index, updateUrl = true) {
  if (!state.filtered.length) return;
  state.current = (index + state.filtered.length) % state.filtered.length;
  const asset = state.filtered[state.current];
  const isVideo = asset.media_type === 'video';
  const full = $('#full');
  const video = $('#videoFull');
  full.classList.toggle('hidden', isVideo);
  video.classList.toggle('hidden', !isVideo);
  if (isVideo) {
    full.src = '';
    full.alt = '';
    video.poster = asset.preview || asset.thumb;
    video.src = asset.file;
    video.setAttribute('aria-label', asset.name);
    video.load();
  } else {
    video.pause();
    video.removeAttribute('src');
    video.removeAttribute('poster');
    video.load();
    full.src = assetSrc(asset);
    full.alt = asset.name;
  }
  $('#filename').textContent = asset.name;
  const resolution = asset.width && asset.height ? `${asset.width}×${asset.height}` : (isVideo ? 'video' : '?×?');
  $('#details').textContent = `${asset.rel} · ${resolution} · ${formatBytes(asset.size)}`;
  document.querySelector('.annotation-toolbar')?.classList.toggle('hidden', isVideo);
  $('#comment').value = asset.comment || '';
  $('#commentState').textContent = '已保存';
  renderReviewDecision(asset);
  renderFamilyRow(asset);
  renderProvenance(asset);
  updateAssetActions(asset);
  $('#historyPanel').classList.add('hidden');
  const notice = $('#previewNotice');
  notice.textContent = asset.preview_error || '';
  notice.classList.toggle('hidden', !asset.preview_error);
  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  cancelAnnotationMode();
  loadAnnotations(asset).catch(showError);
  if (updateUrl && asset.asset_id) history.replaceState(null, '', assetUrl(asset));
  if (asset.is_new) {
    asset.is_new = false;
    apiPost('/api/seen', {collection: asset.collection, rel: asset.rel}).then(refreshReviewState).catch(console.error);
  }
}

function close() {
  modal.classList.add('hidden');
  $('#full').src = '';
  const video = $('#videoFull');
  video.pause();
  video.removeAttribute('src');
  video.removeAttribute('poster');
  video.load();
  state.annotations = [];
  cancelAnnotationMode();
  renderAnnotations();
  if (isQuickReviewOpen()) {
    $('#quickReview').classList.remove('suspended');
    $('#quickReview').setAttribute('aria-hidden', 'false');
    requestAnimationFrame(() => $('#quickReviewApprove')?.focus());
  }
  if (state.active) history.replaceState(null, '', collectionUrl(state.active));
  if (compareModal.classList.contains('hidden') && !isQuickReviewOpen()) document.body.style.overflow = '';
}

const step = amount => openAt(state.current + amount);

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

function updateLocal(assetOrRel, patch) {
  const asset = state.images.find(item => keyFor(item) === assetOrRel || item.asset_id === assetOrRel || item.rel === assetOrRel);
  if (asset) Object.assign(asset, patch);
}

async function review(status) {
  const asset = state.filtered[state.current];
  const comment = $('#comment').value;
  clearTimeout(commentSaveTimer);
  commentSaveTimer = null;
  await apiPost('/api/review', {collection: asset.collection, asset_id: asset.asset_id, status, comment});
  updateLocal(asset.asset_id || asset.rel, {status, comment, is_new: false});
  applyFilter();
  await refreshReviewState();
  const index = state.filtered.findIndex(item => keyFor(item) === keyFor(asset));
  if (index >= 0) openAt(index); else close();
}

let commentSaveTimer = null;

async function saveCommentSnapshot(assetKey, collection, assetId, comment) {
  const current = state.filtered[state.current];
  if (current && keyFor(current) === assetKey) $('#commentState').textContent = 'Saving…';
  await apiPost('/api/comment', {collection, asset_id: assetId, comment});
  updateLocal(assetKey, {comment, is_new: false});
  applyFilter();
  const index = state.filtered.findIndex(item => keyFor(item) === assetKey);
  if (index >= 0 && !modal.classList.contains('hidden')) state.current = index;
  await refreshReviewState();
  const active = state.filtered[state.current];
  if (active && keyFor(active) === assetKey && $('#comment').value === comment) $('#commentState').textContent = 'Saved';
}

function scheduleCommentSave() {
  clearTimeout(commentSaveTimer);
  const asset = state.filtered[state.current];
  if (!asset) return;
  const assetKey = keyFor(asset);
  const collection = asset.collection;
  const assetId = asset.asset_id;
  const comment = $('#comment').value;
  $('#commentState').textContent = 'Unsaved';
  commentSaveTimer = setTimeout(() => {
    commentSaveTimer = null;
    saveCommentSnapshot(assetKey, collection, assetId, comment).catch(showError);
  }, 650);
}

async function bulkReview(status) {
  const assets = allSelectedAssets();
  const rels = assets.map(asset => asset.rel);
  if (!rels.length) return;
  const collections = [...new Set(assets.map(asset => asset.collection))];
  if (collections.length !== 1) throw new Error('跨集合选择不能批量审核；请先进入一个项目场景或集合。');
  await apiPost('/api/review', {collection: collections[0], rels, status});
  for (const asset of assets) updateLocal(asset.asset_id || asset.rel, {status, is_new: false});
  state.selected.clear();
  state.selectionAnchor = null;
  state.selectionAnchorKey = '';
  applyFilter();
  await refreshReviewState();
}

async function undoReview() {
  const asset = state.filtered[state.current];
  const data = await apiPost('/api/undo', {collection: asset.collection, asset_id: asset.asset_id});
  updateLocal(asset.asset_id || asset.rel, {status: data.result.status, comment: data.result.comment});
  renderReviewDecision(state.images.find(item => keyFor(item) === keyFor(asset)));
  $('#comment').value = data.result.comment;
  applyFilter();
  await refreshReviewState();
  await showHistory(asset.asset_id);
}

async function showHistory(assetIdOverride) {
  const asset = assetIdOverride
    ? state.images.find(item => item.asset_id === assetIdOverride || item.rel === assetIdOverride)
    : state.filtered[state.current];
  if (!asset) return;
  const params = new URLSearchParams({collection: asset.collection, asset_id: asset.asset_id || '', rel: asset.rel});
  const response = await fetch('/api/review-history?' + params, {cache: 'no-store'});
  if (!response.ok) throw new Error('History request failed');
  const data = await response.json();
  const panel = $('#historyPanel');
  panel.innerHTML = data.events.length ? data.events.map(event =>
    `<div class="history-event"><strong>${esc(event.action.replaceAll('_', ' '))}</strong><span>${esc(event.created_at)} · ${esc(event.old_status || 'unreviewed')} → ${esc(event.new_status || 'unreviewed')}${event.undone_at ? ' · undone' : ''}</span></div>`
  ).join('') : '<div class="history-event"><span>No review changes yet.</span></div>';
  panel.classList.remove('hidden');
}

async function toggleComplete() {
  if (!state.reviewState) return;
  const path = state.reviewState.complete ? '/api/reopen' : '/api/complete';
  const data = await apiPost(path, {collection: state.active});
  state.reviewState = data.review_state;
  updateReviewState();
}

async function copyPath() {
  const asset = state.filtered[state.current];
  if (!asset) return;
  await navigator.clipboard.writeText(asset.rel);
  $('#summary').textContent = 'Relative path copied';
}

async function copyReviewLink() {
  const asset = state.filtered[state.current];
  if (!asset) return;
  await navigator.clipboard.writeText(location.origin + assetUrl(asset));
  $('#summary').textContent = 'Review link copied';
}

async function loadAnnotations(asset) {
  if (!asset || !asset.asset_id) {
    state.annotations = [];
    renderAnnotations();
    return;
  }
  const params = new URLSearchParams({collection: asset.collection, asset_id: asset.asset_id});
  const response = await fetch('/api/annotations?' + params, {cache: 'no-store'});
  if (!response.ok) throw new Error('Annotation request failed');
  const data = await response.json();
  state.annotations = data.annotations || [];
  renderAnnotations();
}

function renderAnnotations() {
  const layer = $('#annotationLayer');
  const panel = $('#annotationPanel');
  if (!layer || !panel) return;
  layer.querySelectorAll('.annotation-pin,.annotation-region').forEach(element => element.remove());
  const visible = state.annotations;
  visible.forEach((annotation, index) => {
    const element = document.createElement('button');
    element.type = 'button';
    element.dataset.annotationId = annotation.annotation_id;
    element.className = `${annotation.kind === 'region' ? 'annotation-region' : 'annotation-pin'}${annotation.resolved ? ' resolved' : ''}${annotation.stale ? ' stale' : ''}`;
    element.title = annotation.text || `${annotation.kind} annotation ${index + 1}`;
    if (annotation.kind === 'region') {
      element.style.left = `${annotation.x * 100}%`;
      element.style.top = `${annotation.y * 100}%`;
      element.style.width = `${annotation.w * 100}%`;
      element.style.height = `${annotation.h * 100}%`;
      element.setAttribute('aria-label', `Region annotation ${index + 1}: ${annotation.text || 'No note'}`);
    } else {
      element.style.left = `${annotation.x * 100}%`;
      element.style.top = `${annotation.y * 100}%`;
      element.textContent = String(index + 1);
      element.setAttribute('aria-label', `Point annotation ${index + 1}: ${annotation.text || 'No note'}`);
    }
    element.onclick = event => {
      event.stopPropagation();
      const row = panel.querySelector(`[data-annotation-row="${annotation.annotation_id}"]`);
      if (row) row.scrollIntoView({block: 'nearest'});
    };
    layer.appendChild(element);
  });
  panel.classList.toggle('hidden', visible.length === 0);
  panel.innerHTML = visible.map((annotation, index) => {
    const stateLabel = annotation.stale ? 'stale after content change' : (annotation.resolved ? 'resolved' : 'open');
    return `<div class="annotation-row${annotation.stale ? ' stale' : ''}" data-annotation-row="${esc(annotation.annotation_id)}">
      <span class="annotation-index">${index + 1}</span>
      <div class="annotation-copy"><strong>${esc(annotation.text || 'Untitled annotation')}</strong><span>${esc(annotation.kind)} · ${esc(stateLabel)}</span></div>
      <div class="annotation-actions"><button type="button" data-annotation-resolve="${esc(annotation.annotation_id)}">${annotation.resolved ? 'Reopen' : 'Resolve'}</button><button type="button" data-annotation-delete="${esc(annotation.annotation_id)}">Delete</button></div>
    </div>`;
  }).join('');
  panel.querySelectorAll('[data-annotation-resolve]').forEach(button => {
    button.onclick = () => toggleAnnotationResolved(button.dataset.annotationResolve).catch(showError);
  });
  panel.querySelectorAll('[data-annotation-delete]').forEach(button => {
    button.onclick = () => removeAnnotation(button.dataset.annotationDelete).catch(showError);
  });
}

function normalizedPoint(event) {
  const rect = $('#annotationLayer').getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function setAnnotationMode(mode) {
  state.annotationMode = mode || null;
  state.annotationStart = null;
  $('#annotationLayer').classList.toggle('drawing', Boolean(state.annotationMode));
  $('#annotationMode').value = state.annotationMode || '';
  $('#annotationHelp').textContent = state.annotationMode === 'point'
    ? 'Click the exact point you want changed.'
    : state.annotationMode === 'region'
      ? 'Drag a rectangle around the area you want changed.'
      : 'Choose Point or Region when you need precise feedback.';
}

function cancelAnnotationMode() {
  state.annotationMode = null;
  state.annotationStart = null;
  const layer = $('#annotationLayer');
  if (layer) {
    layer.classList.remove('drawing');
    layer.querySelectorAll('.annotation-draft').forEach(element => element.remove());
  }
  if ($('#annotationMode')) $('#annotationMode').value = '';
  if ($('#annotationHelp')) $('#annotationHelp').textContent = 'Choose Point or Region when you need precise feedback.';
}

async function createSpatialAnnotation(kind, geometry) {
  const asset = state.filtered[state.current];
  if (!asset || !asset.asset_id) return;
  const data = await apiPost('/api/annotation', {
    collection: asset.collection,
    asset_id: asset.asset_id,
    action: 'create',
    kind,
    text: $('#annotationText').value,
    ...geometry,
  });
  state.annotations.push(data.annotation);
  $('#annotationText').value = '';
  updateLocal(asset.asset_id, {annotation_count: (asset.annotation_count || 0) + 1});
  renderAnnotations();
  applyFilter();
  cancelAnnotationMode();
}

async function toggleAnnotationResolved(annotationId) {
  const asset = state.filtered[state.current];
  const current = state.annotations.find(annotation => annotation.annotation_id === annotationId);
  if (!asset || !current) return;
  const data = await apiPost('/api/annotation', {
    collection: asset.collection,
    action: 'update',
    annotation_id: annotationId,
    resolved: !current.resolved,
  });
  state.annotations = state.annotations.map(annotation => annotation.annotation_id === annotationId ? data.annotation : annotation);
  const openCount = state.annotations.filter(annotation => !annotation.resolved && !annotation.stale).length;
  updateLocal(asset.asset_id, {annotation_count: openCount});
  renderAnnotations();
  applyFilter();
}

async function removeAnnotation(annotationId) {
  const asset = state.filtered[state.current];
  if (!asset) return;
  await apiPost('/api/annotation', {collection: asset.collection, action: 'delete', annotation_id: annotationId});
  state.annotations = state.annotations.filter(annotation => annotation.annotation_id !== annotationId);
  const openCount = state.annotations.filter(annotation => !annotation.resolved && !annotation.stale).length;
  updateLocal(asset.asset_id, {annotation_count: openCount});
  renderAnnotations();
  applyFilter();
}

function annotationPointerDown(event) {
  if (!state.annotationMode || event.target.closest('.annotation-pin,.annotation-region')) return;
  const point = normalizedPoint(event);
  if (!point) return;
  if (state.annotationMode === 'point') {
    createSpatialAnnotation('point', {x: point.x, y: point.y, w: 0, h: 0}).catch(showError);
    return;
  }
  state.annotationStart = point;
  const draft = document.createElement('div');
  draft.id = 'annotationDraft';
  draft.className = 'annotation-draft';
  draft.style.left = `${point.x * 100}%`;
  draft.style.top = `${point.y * 100}%`;
  draft.style.width = '0';
  draft.style.height = '0';
  $('#annotationLayer').appendChild(draft);
  $('#annotationLayer').setPointerCapture?.(event.pointerId);
}

function annotationPointerMove(event) {
  if (state.annotationMode !== 'region' || !state.annotationStart) return;
  const point = normalizedPoint(event);
  const draft = $('#annotationDraft');
  if (!point || !draft) return;
  const x = Math.min(state.annotationStart.x, point.x);
  const y = Math.min(state.annotationStart.y, point.y);
  const w = Math.abs(point.x - state.annotationStart.x);
  const h = Math.abs(point.y - state.annotationStart.y);
  draft.style.left = `${x * 100}%`;
  draft.style.top = `${y * 100}%`;
  draft.style.width = `${w * 100}%`;
  draft.style.height = `${h * 100}%`;
}

function annotationPointerUp(event) {
  if (state.annotationMode !== 'region' || !state.annotationStart) return;
  const point = normalizedPoint(event);
  const start = state.annotationStart;
  state.annotationStart = null;
  $('#annotationDraft')?.remove();
  if (!point) return;
  const x = Math.min(start.x, point.x);
  const y = Math.min(start.y, point.y);
  const w = Math.abs(point.x - start.x);
  const h = Math.abs(point.y - start.y);
  if (w < 0.01 || h < 0.01) {
    $('#annotationHelp').textContent = 'Drag a larger region.';
    return;
  }
  createSpatialAnnotation('region', {x, y, w, h}).catch(showError);
}

async function groupVariants() {
  const assets = allSelectedAssets();
  if (assets.length < 2) return;
  const collections = [...new Set(assets.map(asset => asset.collection))];
  if (collections.length !== 1) throw new Error('跨集合选择不能建立版本关系。');
  const collection = collections[0];
  const familyIds = [...new Set(assets.map(asset => asset.family_id).filter(Boolean))];
  if (familyIds.length > 1) throw new Error('Selected assets span multiple existing families. Remove them first or group one family at a time.');
  if (familyIds.length === 1) {
    const ungrouped = assets.filter(asset => !asset.family_id);
    if (!ungrouped.length) throw new Error('Those assets are already in the same family.');
    await apiPost('/api/family', {collection, action: 'add', family_id: familyIds[0], asset_ids: ungrouped.map(asset => asset.asset_id)});
  } else {
    const suggested = assets.map(asset => asset.name.replace(/\.[^.]+$/, '').replace(/[-_ ]?(v|variant|concept)?\d+$/i, '')).filter(Boolean)[0] || 'Variant family';
    const name = window.prompt('Name this variant family', suggested);
    if (!name) return;
    await apiPost('/api/family', {collection, action: 'create', name, asset_ids: assets.map(asset => asset.asset_id)});
  }
  await reloadCurrentScope();
}

async function setPreferredFamilyMember() {
  const asset = state.filtered[state.current];
  if (!asset || !asset.family_id) return;
  await apiPost('/api/family', {collection: asset.collection, action: 'prefer', family_id: asset.family_id, asset_id: asset.asset_id});
  await reloadCurrentScope();
}

async function leaveFamily() {
  const asset = state.filtered[state.current];
  if (!asset || !asset.family_id) return;
  await apiPost('/api/family', {collection: asset.collection, action: 'remove', family_id: asset.family_id, asset_ids: [asset.asset_id]});
  await reloadCurrentScope();
}

function openCompareAssets(assets) {
  state.compareAssets = assets.slice(0, 4);
  if (state.compareAssets.length < 2) return;
  state.compareMode = 'side';
  state.compareLinked = true;
  resetCompareView(false);
  $('#overlayRange').value = '50';
  renderCompare();
  compareModal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function openFamilyCompare() {
  const asset = state.filtered[state.current];
  if (!asset || !asset.family_id) return;
  const family = familyById(asset.family_id, asset.collection);
  const members = state.images.filter(item => item.family_id === asset.family_id && item.present !== false);
  members.sort((a, b) => {
    if (a.asset_id === family?.preferred_asset_id) return -1;
    if (b.asset_id === family?.preferred_asset_id) return 1;
    if (a.asset_id === asset.asset_id) return -1;
    if (b.asset_id === asset.asset_id) return 1;
    return (b.mtime || 0) - (a.mtime || 0);
  });
  openCompareAssets(members);
}

function clearSelection({exit = false} = {}) {
  state.selected.clear();
  state.selectionAnchor = null;
  state.selectionAnchorKey = '';
  if (exit) {
    setSelectionMode(false);
    return;
  }
  applyFilter();
}

function allSelectedAssets() {
  return state.images.filter(asset => state.selected.has(keyFor(asset)));
}

function selectedAssets() {
  return allSelectedAssets().slice(0, 4);
}

function resetCompareView(render = true) {
  state.compareTransforms = state.compareAssets.map(() => ({scale: 1, x: 0, y: 0}));
  state.compareDrag = null;
  if (render) applyCompareTransforms();
}

function compareTransform(index) {
  if (!state.compareTransforms[index]) state.compareTransforms[index] = {scale: 1, x: 0, y: 0};
  return state.compareTransforms[index];
}

function applyCompareTransforms() {
  document.querySelectorAll('[data-compare-image]').forEach(image => {
    const index = Number(image.dataset.compareImage);
    const transform = compareTransform(index);
    image.style.transform = `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`;
  });
}

function updateCompareTransform(index, patch) {
  const indices = state.compareLinked ? state.compareAssets.map((_, i) => i) : [index];
  for (const target of indices) {
    const current = compareTransform(target);
    state.compareTransforms[target] = {...current, ...patch};
  }
  applyCompareTransforms();
}

function attachCompareViewportHandlers() {
  document.querySelectorAll('[data-compare-viewport]').forEach(viewport => {
    const index = Number(viewport.dataset.compareViewport);
    viewport.onwheel = event => {
      event.preventDefault();
      const current = compareTransform(index);
      const factor = event.deltaY < 0 ? 1.12 : 0.89;
      const scale = Math.max(1, Math.min(8, current.scale * factor));
      const ratio = scale / current.scale;
      const rect = viewport.getBoundingClientRect();
      const pointerX = event.clientX - (rect.left + rect.width / 2);
      const pointerY = event.clientY - (rect.top + rect.height / 2);
      const x = scale === 1 ? 0 : current.x - pointerX * (ratio - 1);
      const y = scale === 1 ? 0 : current.y - pointerY * (ratio - 1);
      updateCompareTransform(index, {scale, x, y});
    };
    viewport.onpointerdown = event => {
      if (event.button !== 0) return;
      const current = compareTransform(index);
      state.compareDrag = {index, startX: event.clientX, startY: event.clientY, x: current.x, y: current.y};
      viewport.classList.add('dragging');
      viewport.setPointerCapture?.(event.pointerId);
    };
    viewport.onpointermove = event => {
      const drag = state.compareDrag;
      if (!drag || drag.index !== index) return;
      updateCompareTransform(index, {x: drag.x + event.clientX - drag.startX, y: drag.y + event.clientY - drag.startY});
    };
    const finish = () => {
      state.compareDrag = null;
      viewport.classList.remove('dragging');
    };
    viewport.onpointerup = finish;
    viewport.onpointercancel = finish;
    viewport.ondblclick = () => resetCompareView();
  });
}

function renderCompare() {
  const assets = state.compareAssets;
  const gridElement = $('#compareGrid');
  const overlayControl = $('#overlayControl');
  if (state.compareMode !== 'side' && assets.length !== 2) state.compareMode = 'side';
  $('#compareMode').value = state.compareMode;
  [...$('#compareMode').options].forEach(option => {
    option.disabled = option.value !== 'side' && assets.length !== 2;
  });
  overlayControl.classList.toggle('hidden', state.compareMode !== 'overlay');
  $('#compareLink').value = state.compareLinked ? 'linked' : 'independent';

  if (state.compareMode === 'side') {
    $('#compareHint').textContent = 'Scroll to zoom, drag to pan, double-click to reset. Linked view keeps variants aligned.';
    gridElement.className = 'compare-grid';
    gridElement.innerHTML = assets.map((asset, index) =>
      `<article class="compare-item"><div class="compare-viewport" data-compare-viewport="${index}"><img class="compare-pan-image" data-compare-image="${index}" src="${esc(assetSrc(asset))}" alt=""></div><div><strong>${esc(asset.name)}</strong><span>${esc(asset.status || 'unreviewed')}${asset.comment ? ' · ' + esc(asset.comment) : ''}</span></div></article>`
    ).join('');
    attachCompareViewportHandlers();
    applyCompareTransforms();
    return;
  }

  const [left, right] = assets;
  const difference = state.compareMode === 'difference';
  $('#compareHint').textContent = difference
    ? 'Difference view · scroll to zoom and drag to inspect changes.'
    : 'Blend view · scroll to zoom and drag while comparing composition.';
  gridElement.className = 'compare-grid compare-single';
  gridElement.innerHTML = `<div class="compare-stack ${difference ? 'difference' : ''}" data-compare-viewport="0">
    <img class="compare-bottom" data-compare-image="0" src="${esc(assetSrc(left))}" alt="${esc(left.name)}">
    <img class="compare-top" data-compare-image="0" src="${esc(assetSrc(right))}" alt="${esc(right.name)}" style="opacity:${difference ? 1 : Number($('#overlayRange').value) / 100}">
    <div class="compare-stack-labels"><span>${esc(left.name)}</span><span>${esc(right.name)}</span></div>
  </div>`;
  attachCompareViewportHandlers();
  applyCompareTransforms();
}

function setCompareMode(mode) {
  if (mode !== 'side' && state.compareAssets.length !== 2) return;
  state.compareMode = mode;
  if (mode !== 'side') state.compareLinked = true;
  renderCompare();
}

function toggleCompareLinked() {
  state.compareLinked = !state.compareLinked;
  if (state.compareLinked && state.compareAssets.length) {
    const source = {...compareTransform(0)};
    state.compareTransforms = state.compareAssets.map(() => ({...source}));
  }
  renderCompare();
}

function openCompare() {
  openCompareAssets(selectedAssets());
}

function closeCompare() {
  compareModal.classList.add('hidden');
  if (isQuickReviewOpen()) {
    $('#quickReview').classList.remove('suspended');
    $('#quickReview').setAttribute('aria-hidden', 'false');
    requestAnimationFrame(() => $('#quickReviewCompare')?.focus());
  }
  if (modal.classList.contains('hidden') && !isQuickReviewOpen()) document.body.style.overflow = '';
}

function formatEventTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
}

function activityAsset(event) {
  const rel = String(event?.rel || '').trim();
  const assetId = String(event?.asset_id || '').trim();
  return state.images.find(asset => (rel && asset.rel === rel) || (assetId && asset.asset_id === assetId)) || null;
}

function activityPresentation(event) {
  const action = String(event.action || '').toLowerCase();
  const status = String(event.new_status || '').toLowerCase();
  if ((action === 'review' || action === 'batch') && status === 'approved') return {tone: 'approved', mark: '✓', label: 'Review decision'};
  if ((action === 'review' || action === 'batch') && status === 'rejected') return {tone: 'rejected', mark: '×', label: 'Review decision'};
  if ((action === 'review' || action === 'batch') && status === 'maybe') return {tone: 'maybe', mark: '?', label: 'Review decision'};
  if (action === 'asset_added' || action === 'asset_restored') return {tone: 'added', mark: '+', label: 'Collection'};
  if (action === 'comment' || action.startsWith('annotation_')) return {tone: 'note', mark: '•', label: 'Feedback'};
  if (action.startsWith('family_')) return {tone: 'family', mark: '◇', label: 'Variant family'};
  if (action === 'collection_complete') return {tone: 'approved', mark: '✓', label: 'Collection'};
  return {tone: 'neutral', mark: '•', label: action.replaceAll('_', ' ') || 'Activity'};
}

async function loadActivity() {
  if (!state.active) return;
  if (!isSingleCollectionScope()) {
    state.activityEvents = [];
    $('#activitySummary').textContent = '活动记录按集合保存';
    $('#activityList').innerHTML = '<div class="activity-empty">请选择一个具体集合查看审核历史与批注记录。</div>';
    renderWorkspaceState();
    return;
  }
  const response = await fetch(`/api/activity?collection=${encodeURIComponent(state.active)}&limit=200`, {cache: 'no-store'});
  if (!response.ok) throw new Error('Activity request failed');
  const data = await response.json();
  state.activityEvents = data.events || [];
  $('#activitySummary').textContent = `${state.activityEvents.length} recent events`;
  $('#activityList').innerHTML = state.activityEvents.length
    ? [...state.activityEvents].reverse().map(event => {
        const presentation = activityPresentation(event);
        const asset = activityAsset(event);
        const thumbnail = asset ? `<img class="activity-thumb" src="${esc(asset.thumb)}" alt="">` : '';
        return `<article class="activity-event ${presentation.tone}"><span class="activity-marker" aria-hidden="true">${presentation.mark}</span><div class="activity-body">${thumbnail}<div class="activity-copy"><strong>${esc(event.summary || event.action)}</strong><span>${esc(presentation.label)}</span></div></div><time datetime="${esc(event.created_at || '')}">${esc(formatEventTime(event.created_at))}</time></article>`;
      }).join('')
    : '<div class="activity-empty">No activity yet.</div>';
  renderWorkspaceState();
}

async function setWorkspaceTab(tab) {
  const next = ['gallery', 'activity', 'approved'].includes(tab) ? tab : 'gallery';
  const previous = state.workspaceTab;
  state.activeSavedView = '';

  if (next === 'activity') {
    if (previous === 'approved' && $('#filter').value === 'approved') {
      $('#filter').value = state.galleryFilterBeforeApproved || 'all';
    }
    state.workspaceTab = 'activity';
    state.view = 'activity';
    renderViewMenu();
    $('#galleryView').classList.add('hidden');
    $('#activityView').classList.remove('hidden');
    $('#approvedPanel').classList.add('hidden');
    await loadActivity();
    renderWorkspaceState();
    return;
  }

  state.view = 'gallery';
  state.workspaceTab = next;
  $('#galleryView').classList.remove('hidden');
  $('#activityView').classList.add('hidden');
  if (next === 'approved') {
    if (previous !== 'approved' && $('#filter').value !== 'approved') state.galleryFilterBeforeApproved = $('#filter').value || 'all';
    $('#filter').value = 'approved';
  } else if (previous === 'approved' && $('#filter').value === 'approved') {
    $('#filter').value = state.galleryFilterBeforeApproved || 'all';
  }
  renderViewMenu();
  applyFilter();
  updateCollectionActions();
  renderWorkspaceState();
}

function syncWorkspaceFromAssetControls() {
  state.view = 'gallery';
  state.activeSavedView = '';
  state.workspaceTab = $('#filter').value === 'approved' ? 'approved' : 'gallery';
  if (state.workspaceTab === 'gallery') state.galleryFilterBeforeApproved = $('#filter').value || 'all';
  $('#galleryView').classList.remove('hidden');
  $('#activityView').classList.add('hidden');
  renderViewMenu();
}

async function setView(view) {
  if (view.startsWith('saved:')) {
    applySavedView(view.slice(6));
    return;
  }
  await setWorkspaceTab(view === 'activity' ? 'activity' : 'gallery');
}

async function switchCollection(slug) {
  if (!slug || (slug === state.active && isSingleCollectionScope())) return;
  const tab = state.workspaceTab;
  history.pushState(null, '', collectionUrl(slug));
  await load(slug);
  await setWorkspaceTab(tab);
}

function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2) + '\n'], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function exportHandoff() {
  if (!state.active || !isSingleCollectionScope()) return;
  const response = await fetch(`/api/handoff?collection=${encodeURIComponent(state.active)}`, {cache: 'no-store'});
  if (!response.ok) throw new Error('Approved handoff request failed');
  const payload = await response.json();
  downloadJson(`${state.active}-approved-handoff.json`, payload);
}

async function handleCollectionAction(value) {
  if (!value) return;
  if (value === 'select-all') selectAllVisible();
  else if (value === 'save-view') saveCurrentView();
  else if (value === 'delete-view') deleteActiveSavedView();
  else if (value === 'report') window.open(`/report?collection=${encodeURIComponent(state.active)}&present=1&refresh=1`, '_blank', 'noopener');
  else if (value === 'handoff') await exportHandoff();
  else if (value === 'refresh') await reloadCurrentScope(true);
  else if (value === 'complete' || value === 'reopen') await toggleComplete();
  updateCollectionActions();
}

async function handleSelectionAction(value) {
  if (!value) return;
  if (value === 'approve') await bulkReview('approved');
  else if (value === 'maybe') await bulkReview('maybe');
  else if (value === 'reject') await bulkReview('rejected');
  else if (value === 'clear-review') await bulkReview('');
  else if (value === 'compare') openCompare();
  else if (value === 'group') await groupVariants();
  else if (value === 'clear-selection') clearSelection();
  updateBulk();
}

async function handleAssetAction(value) {
  if (!value) return;
  const asset = state.filtered[state.current];
  if (!asset) return;
  if (value === 'history') await showHistory();
  else if (value === 'undo') await undoReview();
  else if (value === 'copy-path') await copyPath();
  else if (value === 'copy-link') await copyReviewLink();
  else if (value === 'open-original') window.open(asset.file, '_blank', 'noopener');
  updateAssetActions(asset);
}

async function handleFamilyAction(value) {
  if (!value) return;
  if (value === 'compare') openFamilyCompare();
  else if (value === 'prefer') await setPreferredFamilyMember();
  else if (value === 'leave') await leaveFamily();
}

function paletteCommands() {
  const commands = [
    {label: 'View: Gallery', run: () => setWorkspaceTab('gallery')},
    {label: 'View: Activity', run: () => setWorkspaceTab('activity')},
    {label: 'View: Approved', run: () => setWorkspaceTab('approved')},
    {label: 'Focus search', run: () => $('#search').focus()},
    {label: 'Save current view…', run: () => saveCurrentView()},
    {label: 'Refresh collection', run: () => reloadCurrentScope(true)},
  ];
  if (isSingleCollectionScope()) {
    commands.push(
      {label: 'Open review report', run: () => window.open(`/report?collection=${encodeURIComponent(state.active)}&present=1&refresh=1`, '_blank', 'noopener')},
      {label: 'Export approved handoff', run: () => exportHandoff()},
    );
  }
  for (const saved of state.savedViews) commands.push({label: `Saved view: ${saved.name}`, run: () => applySavedView(saved.id)});
  if (state.filtered.length) commands.push({label: `Select all visible (${state.filtered.length})`, run: () => selectAllVisible()});
  if (state.selected.size >= 2) commands.push({label: 'Compare selected assets', run: () => openCompare()});
  if (state.reviewState && !state.reviewState.scan_incomplete && !state.reviewState.empty) {
    if (state.reviewState.complete) commands.push({label: 'Reopen review', run: () => toggleComplete()});
    else if (!state.reviewState.unreviewed) commands.push({label: 'Mark review complete', run: () => toggleComplete()});
  }
  if (!modal.classList.contains('hidden')) {
    commands.push(
      {label: 'Asset: Set Approved', run: () => review('approved')},
      {label: 'Asset: Set Maybe', run: () => review('maybe')},
      {label: 'Asset: Set Rejected', run: () => review('rejected')},
      {label: 'Asset: Clear review state', run: () => review('')},
      {label: 'Asset: Review history', run: () => showHistory()},
      {label: 'Asset: Copy review link', run: () => copyReviewLink()},
    );
  }
  return commands;
}

function closeCommandPalette() {
  commandPalette.classList.add('hidden');
  $('#commandSearch').value = '';
  state.paletteMatches = [];
  state.paletteIndex = 0;
}

function renderCommandPalette() {
  const query = $('#commandSearch').value.trim().toLowerCase();
  state.paletteMatches = paletteCommands().filter(command => !query || command.label.toLowerCase().includes(query));
  state.paletteIndex = Math.max(0, Math.min(state.paletteIndex, Math.max(0, state.paletteMatches.length - 1)));
  $('#commandList').innerHTML = state.paletteMatches.length
    ? state.paletteMatches.map((command, index) => `<button type="button" role="option" class="command-row${index === state.paletteIndex ? ' active' : ''}" data-command="${index}">${esc(command.label)}</button>`).join('')
    : '<span class="command-empty">No matching commands.</span>';
  $('#commandList').querySelectorAll('[data-command]').forEach(button => {
    button.onclick = () => runPaletteCommand(Number(button.dataset.command));
  });
}

function openCommandPalette() {
  state.paletteIndex = 0;
  commandPalette.classList.remove('hidden');
  renderCommandPalette();
  $('#commandSearch').focus();
}

function runPaletteCommand(index) {
  const command = state.paletteMatches[index];
  if (!command) return;
  closeCommandPalette();
  Promise.resolve(command.run()).catch(showError);
}

$('#collection').onchange = () => switchCollection($('#collection').value).catch(showError);
$('#projectFilter').onchange = () => {
  const project = $('#projectFilter').value;
  if (project === '__all__') setAllProjectsScope().catch(showError);
  else if (project) setProjectScope(project).catch(showError);
  else load(state.active).catch(showError);
};
$('#sceneFilter').onchange = () => {
  const scene = $('#sceneFilter').value;
  const project = $('#projectFilter').value && $('#projectFilter').value !== '__all__'
    ? $('#projectFilter').value
    : collectionTaxonomy(state.active).project;
  if (scene) setSceneScope(project, scene).catch(showError);
  else if ($('#projectFilter').value === '__all__') setAllProjectsScope().catch(showError);
  else if ($('#projectFilter').value) setProjectScope(project).catch(showError);
  else load(state.active).catch(showError);
};
$('#useFilter').onchange = () => { syncWorkspaceFromAssetControls(); clearActiveSavedView(); applyFilter(); };
$('#filter').onchange = () => { syncWorkspaceFromAssetControls(); applyFilter(); updateCollectionActions(); };
$('#sort').onchange = () => { if (state.workspaceTab === 'activity') syncWorkspaceFromAssetControls(); clearActiveSavedView(); applyFilter(); };
$('#search').oninput = () => { if (state.workspaceTab === 'activity') syncWorkspaceFromAssetControls(); clearActiveSavedView(); applyFilter(); updateCollectionActions(); };
$('#view').onchange = () => setView($('#view').value).catch(showError);
$('#quickReviewLaunch').onclick = () => startQuickReview();
$('#closeQuickReview').onclick = () => closeQuickReview();
$('#quickReviewPrevious').onclick = () => quickReviewMove(-1);
$('#quickReviewNext').onclick = () => quickReviewMove(1);
$('#quickReviewApprove').onclick = () => quickReviewDecision('approved').catch(showError);
$('#quickReviewMaybe').onclick = () => quickReviewDecision('maybe').catch(showError);
$('#quickReviewReject').onclick = () => quickReviewDecision('rejected').catch(showError);
$('#quickReviewUndo').onclick = () => quickReviewUndo().catch(showError);
$('#quickReviewDetailsButton').onclick = () => openQuickReviewDetails();
$('#quickReviewCompare').onclick = () => compareQuickReviewAssets();
$('#quickReviewComment').oninput = scheduleQuickReviewCommentSave;
$('#selectionMode').onclick = () => setSelectionMode(!state.selectionMode);
$('#selectAllVisible').onclick = () => selectAllVisible();
$('#clearSelection').onclick = () => clearSelection();
document.querySelectorAll('[data-workspace-tab]').forEach(button => {
  button.onclick = () => setWorkspaceTab(button.dataset.workspaceTab).catch(showError);
});
$('#approvedHandoff').onclick = () => exportHandoff().catch(showError);
$('#approvedReport').onclick = () => window.open(`/report?collection=${encodeURIComponent(state.active)}&present=1&refresh=1`, '_blank', 'noopener');
$('#actions').onchange = () => handleCollectionAction($('#actions').value).catch(showError);
$('#selectionAction').onchange = () => handleSelectionAction($('#selectionAction').value).catch(showError);
$('#reviewStatus').onchange = () => review($('#reviewStatus').value).catch(showError);
$('#assetActions').onchange = () => handleAssetAction($('#assetActions').value).catch(showError);
$('#familyActions').onchange = () => handleFamilyAction($('#familyActions').value).catch(showError);
$('#annotationMode').onchange = () => setAnnotationMode($('#annotationMode').value);
$('#comment').oninput = scheduleCommentSave;
$('#close').onclick = close;
$('#prev').onclick = () => step(-1);
$('#next').onclick = () => step(1);
$('#annotationLayer').onpointerdown = annotationPointerDown;
$('#annotationLayer').onpointermove = annotationPointerMove;
$('#annotationLayer').onpointerup = annotationPointerUp;
$('#compareMode').onchange = () => setCompareMode($('#compareMode').value);
$('#compareLink').onchange = () => {
  const linked = $('#compareLink').value === 'linked';
  if (state.compareLinked !== linked) toggleCompareLinked();
};
$('#compareActions').onchange = () => {
  if ($('#compareActions').value === 'reset') resetCompareView();
  $('#compareActions').value = '';
};
$('#closeCompare').onclick = closeCompare;
$('#commandSearch').oninput = () => { state.paletteIndex = 0; renderCommandPalette(); };
$('#commandSearch').onkeydown = event => {
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    state.paletteIndex = Math.min(state.paletteMatches.length - 1, state.paletteIndex + 1);
    renderCommandPalette();
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    state.paletteIndex = Math.max(0, state.paletteIndex - 1);
    renderCommandPalette();
  } else if (event.key === 'Enter') {
    event.preventDefault();
    runPaletteCommand(state.paletteIndex);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closeCommandPalette();
  }
};
commandPalette.onclick = event => { if (event.target === commandPalette) closeCommandPalette(); };
$('#overlayRange').oninput = () => {
  const top = $('#compareGrid .compare-top');
  if (top && state.compareMode === 'overlay') top.style.opacity = Number($('#overlayRange').value) / 100;
};

window.onpopstate = () => load(collectionFromLocation()).catch(showError);
document.onkeydown = event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    commandPalette.classList.contains('hidden') ? openCommandPalette() : closeCommandPalette();
    return;
  }
  if (!commandPalette.classList.contains('hidden')) {
    if (event.key === 'Escape') closeCommandPalette();
    return;
  }
  if (!compareModal.classList.contains('hidden')) {
    if (event.key === 'Escape') closeCompare();
    return;
  }
  const editing = event.target && ['TEXTAREA', 'INPUT', 'SELECT'].includes(event.target.tagName);
  if (!modal.classList.contains('hidden')) {
    if (editing) return;
    if (event.key === 'Escape' && state.annotationMode) cancelAnnotationMode();
    else if (event.key === 'Escape') close();
    else if (event.key === 'ArrowLeft') step(-1);
    else if (event.key === 'ArrowRight') step(1);
    else if (event.key.toLowerCase() === 'a') review('approved').catch(showError);
    else if (event.key.toLowerCase() === 'm') review('maybe').catch(showError);
    else if (event.key.toLowerCase() === 'r') review('rejected').catch(showError);
    return;
  }
  if (isQuickReviewOpen()) {
    if (editing) return;
    if (event.repeat && (((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') || ['1', '2', '3'].includes(event.key))) {
      event.preventDefault();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      quickReviewUndo().catch(showError);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeQuickReview();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      quickReviewMove(-1);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      quickReviewMove(1);
    } else if (event.key === '1') {
      event.preventDefault();
      quickReviewDecision('approved').catch(showError);
    } else if (event.key === '2') {
      event.preventDefault();
      quickReviewDecision('maybe').catch(showError);
    } else if (event.key === '3') {
      event.preventDefault();
      quickReviewDecision('rejected').catch(showError);
    }
    return;
  }
  if (state.selectionMode && !editing) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
      event.preventDefault();
      selectAllVisible();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      clearSelection({exit: true});
      return;
    }
  }
};

async function pollCatalog() {
  if (document.hidden || !state.active || !isSingleCollectionScope() || state.selected.size) return;
  if (isQuickReviewOpen()) return;
  if (!modal.classList.contains('hidden') || !compareModal.classList.contains('hidden')) return;
  const scopeRequest = state.scopeRequest;
  const active = state.active;
  const params = new URLSearchParams({collection: active});
  const response = await fetch('/api/gallery?' + params, {cache: 'no-store'});
  if (!response.ok) return;
  const data = await response.json();
  if (scopeRequest !== state.scopeRequest || !isSingleCollectionScope() || state.active !== active) return;
  const generation = Number((data.scan || {}).generation || 0);
  if (generation !== state.catalogGeneration) await load(active);
}

function showError(error) {
  console.error(error);
  $('#summary').textContent = error.message || 'Viewer error';
}

(async () => {
  try {
    loadSavedViews();
    renderViewMenu();
    await session();
    await load(collectionFromLocation());
    setInterval(() => pollCatalog().catch(error => console.error(error)), 2000);
  } catch (error) {
    showError(error);
  }
})();
