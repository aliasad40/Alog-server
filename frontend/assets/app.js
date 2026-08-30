/* Network Log Server -- operator console.
 *
 * Deliberately dependency-free. There is no bundler, no npm install and no
 * CDN in the install path: the installer copies these three files and nginx
 * serves them. That removes Node from the deployment entirely and means the
 * product installs on an air-gapped ISP network.
 *
 * All values are inserted with textContent, never innerHTML, so a subscriber
 * id or interface name coming out of the database can never execute.
 */
'use strict';

const $  = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const state = {
  user: null,
  offset: 0,
  limit: 100,
  orderBy: 'timestamp',
  descending: true,
  lastRows: [],
  preset: 'public',
};

/* ---------------------------------------------------------------- http -- */

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options);
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && state.user) return showLogin('Your session expired. Sign in again.');
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${res.status})`);
  }
  return data;
}

function flash(id, text, kind = 'error') {
  const node = $(id);
  node.textContent = text;
  node.className = `msg msg-${kind}`;
  if (kind === 'ok') setTimeout(() => node.classList.add('hidden'), 3200);
}
const clearFlash = (id) => $(id).classList.add('hidden');

/* ------------------------------------------------------------ branding -- */

async function loadBranding() {
  try {
    const b = await api('/api/settings/branding');
    $('login-company').textContent = b.company_name;
    $('app-company').textContent = b.company_name;
    document.title = `${b.company_name} — Log Server`;
    for (const id of ['login-logo', 'app-logo']) {
      const img = $(id);
      if (b.logo_url) { img.src = b.logo_url; img.alt = b.company_name; img.classList.remove('hidden'); }
      else img.classList.add('hidden');
    }
    $('s-company').value = b.company_name;
  } catch (_) { /* the login page still works without branding */ }
}

/* --------------------------------------------------------------- views -- */

function showLogin(message) {
  state.user = null;
  $('app-view').classList.add('hidden');
  $('login-view').classList.remove('hidden');
  $('login-credit').classList.remove('hidden');
  if (message) flash('login-msg', message);
  $('login-username').focus();
}

function showApp(user) {
  state.user = user;
  $('login-view').classList.add('hidden');
  $('login-credit').classList.add('hidden');
  $('app-view').classList.remove('hidden');
  $('who').textContent = user.username;
  setDefaultTimeRange();
  if (user.must_change_password) {
    openModal('settings-modal');
    flash('settings-msg', 'You are still using the password from installation. Change it now.', 'warn');
  }
}

function switchTab(name) {
  for (const tab of document.querySelectorAll('.tab'))
    tab.classList.toggle('active', tab.dataset.tab === name);
  $('tab-search').classList.toggle('hidden', name !== 'search');
  $('tab-routers').classList.toggle('hidden', name !== 'routers');
  if (name === 'routers') loadRouters();
}

const openModal  = (id) => $(id).classList.remove('hidden');
const closeModal = (id) => $(id).classList.add('hidden');

/* ---------------------------------------------------------------- auth -- */

async function doLogin() {
  clearFlash('login-msg');
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  if (!username || !password) return flash('login-msg', 'Enter your username and password.');
  $('login-submit').disabled = true;
  try {
    const user = await api('/api/auth/login', { method: 'POST', body: { username, password } });
    $('login-password').value = '';
    await loadBranding();
    showApp(user);
  } catch (err) {
    flash('login-msg', err.message);
  } finally {
    $('login-submit').disabled = false;
  }
}

/* -------------------------------------------------------------- search -- */

const pad = (n) => String(n).padStart(2, '0');
const toLocalInput = (d) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
  `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

function setDefaultTimeRange() {
  const now = new Date();
  const from = new Date(now.getTime() - 24 * 3600 * 1000);
  $('f-to').value = toLocalInput(now);
  $('f-from').value = toLocalInput(from);
}

/* The presets are the three investigations this product exists to serve.
   Each one shows only the fields that keep the query on an index. */
const PRESETS = {
  public:     ['public', 'other'],
  subscriber: ['subscriber', 'other'],
  private:    ['private', 'subscriber', 'other'],
  all:        ['public', 'private', 'subscriber', 'dest', 'other'],
};

function applyPreset(name) {
  state.preset = name;
  for (const btn of document.querySelectorAll('.preset'))
    btn.classList.toggle('active', btn.dataset.preset === name);
  const groups = PRESETS[name];
  for (const node of document.querySelectorAll('#filters [data-group]'))
    node.classList.toggle('hidden', !groups.includes(node.dataset.group));
}

function collectFilters() {
  const value = (id) => {
    const raw = $(id).value.trim();
    return raw === '' ? null : raw;
  };
  const num = (id) => {
    const raw = value(id);
    return raw === null ? null : Number(raw);
  };
  const body = {
    time_from: value('f-from'),
    time_to: value('f-to'),
    public_ip: value('f-public-ip'),
    public_port: num('f-public-port'),
    private_ip: value('f-private-ip'),
    private_port: num('f-private-port'),
    dest_ip: value('f-dest-ip'),
    dest_port: num('f-dest-port'),
    protocol: value('f-protocol'),
    router_ip: value('f-router'),
    subscriber_id: value('f-subscriber'),
    subscriber_partial: $('f-partial').checked,
    limit: state.limit,
    offset: state.offset,
    order_by: state.orderBy,
    descending: state.descending,
  };
  // Fields hidden by the active preset must not silently filter the results.
  const groups = PRESETS[state.preset];
  const groupOf = {
    public_ip: 'public', public_port: 'public',
    private_ip: 'private', private_port: 'private',
    dest_ip: 'dest', dest_port: 'dest',
    subscriber_id: 'subscriber',
  };
  for (const [field, group] of Object.entries(groupOf))
    if (!groups.includes(group)) body[field] = null;
  return body;
}

function renderRows(rows) {
  const tbody = $('results');
  tbody.textContent = '';
  for (const row of rows) {
    const tr = el('tr');

    tr.appendChild(el('td', '', row.timestamp));

    const sub = el('td');
    sub.appendChild(el('span', 'subscriber', row.subscriber_id || '—'));
    tr.appendChild(sub);

    const priv = el('td', 'col-inside');
    priv.appendChild(document.createTextNode(row.private_ip));
    priv.appendChild(el('span', 'port', ':' + row.private_port));
    tr.appendChild(priv);

    const pub = el('td', 'col-outside');
    pub.appendChild(document.createTextNode(row.public_ip));
    pub.appendChild(el('span', 'port', ':' + row.public_port));
    tr.appendChild(pub);

    const dst = el('td');
    dst.appendChild(document.createTextNode(row.dest_ip));
    dst.appendChild(el('span', 'port', ':' + row.dest_port));
    tr.appendChild(dst);

    const proto = el('td');
    if (row.protocol) proto.appendChild(el('span', 'proto-tag', row.protocol));
    tr.appendChild(proto);

    tr.appendChild(el('td', '', row.router_ip));
    tr.appendChild(el('td', 'port', row.session_end_time || '—'));
    tbody.appendChild(tr);
  }
  $('results-empty').classList.toggle('hidden', rows.length > 0);
  if (rows.length === 0) {
    $('results-empty').textContent = '';
    $('results-empty').appendChild(el('strong', '', 'No matching logs'));
    $('results-empty').appendChild(document.createTextNode(
      'Widen the time range, or check that the router is authorised and sending.'));
  }
}

async function runSearch(resetOffset = true) {
  clearFlash('search-msg');
  if (resetOffset) state.offset = 0;
  state.limit = Number($('f-limit').value);
  $('do-search').disabled = true;
  $('result-meta').textContent = 'Searching…';
  try {
    const data = await api('/api/search', { method: 'POST', body: collectFilters() });
    state.lastRows = data.rows;
    renderRows(data.rows);
    $('result-meta').textContent =
      `${data.rows.length} row${data.rows.length === 1 ? '' : 's'} · ` +
      `${data.elapsed_ms} ms · ${data.rows_scanned.toLocaleString()} scanned`;
    $('pager').classList.toggle('hidden', data.rows.length === 0 && state.offset === 0);
    $('page-prev').disabled = state.offset === 0;
    $('page-next').disabled = !data.has_more;
    $('page-info').textContent =
      `Rows ${state.offset + 1}–${state.offset + data.rows.length}` + (data.has_more ? '' : ' (end)');
  } catch (err) {
    $('result-meta').textContent = '';
    flash('search-msg', err.message);
  } finally {
    $('do-search').disabled = false;
  }
}

async function runCount() {
  $('do-count').disabled = true;
  const previous = $('result-meta').textContent;
  $('result-meta').textContent = 'Counting…';
  try {
    const data = await api('/api/search/count', { method: 'POST', body: collectFilters() });
    $('result-meta').textContent = `${data.count.toLocaleString()} matching rows in total`;
  } catch (err) {
    $('result-meta').textContent = previous;
    flash('search-msg', err.message);
  } finally {
    $('do-count').disabled = false;
  }
}

function exportCsv() {
  if (!state.lastRows.length) return flash('search-msg', 'Run a search before exporting.');
  const cols = ['timestamp', 'subscriber_id', 'private_ip', 'private_port', 'public_ip',
                'public_port', 'dest_ip', 'dest_port', 'protocol', 'router_ip',
                'session_start_time', 'session_end_time'];
  const escape = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    // Neutralise spreadsheet formula injection: a subscriber id starting
    // with = or + would otherwise execute when the CSV is opened in Excel.
    const safe = /^[=+\-@\t\r]/.test(s) ? "'" + s : s;
    return `"${safe.replace(/"/g, '""')}"`;
  };
  const lines = [cols.join(','), ...state.lastRows.map((r) => cols.map((c) => escape(r[c])).join(','))];
  const blob = new Blob([lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = el('a');
  a.href = url;
  a.download = `nat-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ------------------------------------------------------------- routers -- */

async function loadRouters() {
  clearFlash('routers-msg');
  try {
    const routers = await api('/api/routers');
    const tbody = $('routers-body');
    tbody.textContent = '';
    for (const r of routers) {
      const tr = el('tr');
      tr.appendChild(el('td', '', r.name));
      tr.appendChild(el('td', 'mono', r.ip_address));

      const status = el('td');
      status.appendChild(el('span', `status-dot ${r.enabled ? 'dot-on' : 'dot-off'}`));
      status.appendChild(document.createTextNode(r.enabled ? 'Enabled' : 'Disabled'));
      tr.appendChild(status);

      tr.appendChild(el('td', '', r.description || '—'));

      const actions = el('td');
      const wrap = el('div', 'row-actions');
      const toggle = el('button', 'btn btn-sm', r.enabled ? 'Disable' : 'Enable');
      toggle.onclick = () => updateRouter(r.id, { enabled: !r.enabled });
      const edit = el('button', 'btn btn-sm', 'Edit');
      edit.onclick = () => openRouterModal(r);
      const del = el('button', 'btn btn-sm btn-danger', 'Delete');
      del.onclick = () => deleteRouter(r);
      wrap.append(toggle, edit, del);
      actions.appendChild(wrap);
      tr.appendChild(actions);
      tbody.appendChild(tr);
    }
    $('routers-empty').classList.toggle('hidden', routers.length > 0);
  } catch (err) {
    flash('routers-msg', err.message);
  }
}

function openRouterModal(router) {
  clearFlash('router-form-msg');
  $('router-modal-title').textContent = router ? 'Edit router' : 'Add router';
  $('r-id').value = router ? router.id : '';
  $('r-name').value = router ? router.name : '';
  $('r-ip').value = router ? router.ip_address : '';
  $('r-desc').value = router ? router.description : '';
  $('r-enabled').checked = router ? !!router.enabled : true;
  openModal('router-modal');
  $('r-name').focus();
}

async function saveRouter() {
  clearFlash('router-form-msg');
  const id = $('r-id').value;
  const body = {
    name: $('r-name').value.trim(),
    ip_address: $('r-ip').value.trim(),
    description: $('r-desc').value.trim(),
    enabled: $('r-enabled').checked,
  };
  if (!body.name || !body.ip_address)
    return flash('router-form-msg', 'A name and IP address are both required.');
  try {
    if (id) await api(`/api/routers/${id}`, { method: 'PUT', body });
    else await api('/api/routers', { method: 'POST', body });
    closeModal('router-modal');
    loadRouters();
  } catch (err) {
    flash('router-form-msg', err.message);
  }
}

async function updateRouter(id, body) {
  try { await api(`/api/routers/${id}`, { method: 'PUT', body }); loadRouters(); }
  catch (err) { flash('routers-msg', err.message); }
}

async function deleteRouter(router) {
  if (!confirm(`Remove ${router.name} (${router.ip_address})?\n\n` +
               'Logs already collected are kept. New logs from this address will be discarded.'))
    return;
  try { await api(`/api/routers/${router.id}`, { method: 'DELETE' }); loadRouters(); }
  catch (err) { flash('routers-msg', err.message); }
}

/* ------------------------------------------------------------ settings -- */

const bytes = (n) => {
  if (!n) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
};

function stat(grid, key, value, sub, tone) {
  const card = el('div', `card stat${tone ? ' ' + tone : ''}`);
  card.appendChild(el('div', 'k', key));
  card.appendChild(el('div', 'v', value));
  if (sub) card.appendChild(el('div', 'sub', sub));
  grid.appendChild(card);
}

async function loadStatus() {
  const grid = $('status-grid');
  try {
    const s = await api('/api/system/status');
    grid.textContent = '';
    stat(grid, 'Received', s.ingest.received.toLocaleString(), `${s.ingest.received_per_sec}/s`);
    stat(grid, 'Stored', s.ingest.stored.toLocaleString(), `${s.ingest.stored_per_sec}/s`);
    stat(grid, 'Parsed', `${s.ingest.parse_success_pct}%`,
         `${s.ingest.parser_errors.toLocaleString()} errors`);
    stat(grid, 'Dropped', s.ingest.dropped_total.toLocaleString(),
         `${s.ingest.unknown_router.toLocaleString()} unknown router`,
         s.ingest.dropped_total > 0 ? 'bad' : 'good');
    stat(grid, 'Queue depth', s.queue.length.toLocaleString(),
         `${s.queue.in_flight.toLocaleString()} in flight`,
         s.queue.length > s.queue.max_length * 0.5 ? 'bad' : '');
    stat(grid, 'Redis', s.queue.healthy ? 'Up' : 'Down', bytes(s.queue.redis_memory_bytes),
         s.queue.healthy ? 'good' : 'bad');
    stat(grid, 'ClickHouse', s.database.healthy ? 'Up' : 'Down',
         `${s.database.insert_errors} insert errors`, s.database.healthy ? 'good' : 'bad');
    stat(grid, 'Rows stored', (s.database.rows || 0).toLocaleString(),
         `${s.database.parts || 0} parts`);
    stat(grid, 'On disk', bytes(s.database.compressed_bytes),
         `${s.database.compression_ratio}× · ${s.database.bytes_per_row} B/row`);
    stat(grid, 'Disk free', bytes(s.host.disk_free), `${s.host.disk_percent}% used`,
         s.host.disk_percent > 85 ? 'bad' : '');
    stat(grid, 'CPU', `${s.host.cpu_percent}%`, `load ${s.host.load.join(' ')}`);
    stat(grid, 'Memory', `${s.host.memory_percent}%`, `${s.routers.authorised} routers authorised`);
  } catch (err) {
    grid.textContent = '';
    grid.appendChild(el('div', 'sub', `Status unavailable: ${err.message}`));
  }
}

async function loadRetention() {
  try {
    const r = await api('/api/settings/retention');
    $('s-hot').value = r.hot_days;
    $('s-retain').value = r.retention_months;
  } catch (_) { /* shown by the status panel already */ }
}

async function saveBranding() {
  clearFlash('settings-msg');
  try {
    await api('/api/settings/branding', { method: 'PUT', body: { company_name: $('s-company').value.trim() } });
    const file = $('s-logo').files[0];
    if (file) {
      const form = new FormData();
      form.append('file', file);
      await api('/api/settings/logo', { method: 'POST', body: form });
      $('s-logo').value = '';
    }
    await loadBranding();
    flash('settings-msg', 'Branding updated.', 'ok');
  } catch (err) { flash('settings-msg', err.message); }
}

/* ------------------------------------------------------------- wiring -- */

function bind() {
  $('login-submit').onclick = doLogin;
  for (const id of ['login-username', 'login-password'])
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });

  $('logout').onclick = async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
    showLogin();
  };

  for (const tab of document.querySelectorAll('.tab'))
    tab.onclick = () => switchTab(tab.dataset.tab);

  for (const preset of document.querySelectorAll('.preset'))
    preset.onclick = () => applyPreset(preset.dataset.preset);

  $('do-search').onclick = () => runSearch(true);
  $('do-count').onclick = runCount;
  $('do-export').onclick = exportCsv;
  $('clear-filters').onclick = () => {
    for (const input of document.querySelectorAll('#filters input')) input.value = '';
    $('f-protocol').value = '';
    $('f-partial').checked = false;
    setDefaultTimeRange();
    $('results').textContent = '';
    $('result-meta').textContent = '';
    $('pager').classList.add('hidden');
    $('results-empty').classList.remove('hidden');
  };

  for (const input of document.querySelectorAll('#filters input'))
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(true); });

  $('page-prev').onclick = () => {
    state.offset = Math.max(0, state.offset - state.limit);
    runSearch(false);
  };
  $('page-next').onclick = () => { state.offset += state.limit; runSearch(false); };

  for (const th of document.querySelectorAll('th.sortable')) {
    th.onclick = () => {
      const column = th.dataset.sort;
      if (state.orderBy === column) state.descending = !state.descending;
      else { state.orderBy = column; state.descending = true; }
      for (const other of document.querySelectorAll('th.sortable')) {
        const arrow = other.querySelector('.arrow');
        if (arrow) arrow.remove();
      }
      th.appendChild(el('span', 'arrow', state.descending ? '↓' : '↑'));
      runSearch(true);
    };
  }

  $('add-router').onclick = () => openRouterModal(null);
  $('save-router').onclick = saveRouter;

  $('open-settings').onclick = () => { openModal('settings-modal'); loadStatus(); loadRetention(); };
  $('save-branding').onclick = saveBranding;
  $('remove-logo').onclick = async () => {
    try { await api('/api/settings/logo', { method: 'DELETE' }); await loadBranding();
          flash('settings-msg', 'Logo removed.', 'ok'); }
    catch (err) { flash('settings-msg', err.message); }
  };
  $('save-retention').onclick = async () => {
    clearFlash('settings-msg');
    try {
      await api('/api/settings/retention', {
        method: 'PUT',
        body: { hot_days: Number($('s-hot').value), retention_months: Number($('s-retain').value) },
      });
      flash('settings-msg', 'Retention updated.', 'ok');
    } catch (err) { flash('settings-msg', err.message); }
  };
  $('save-password').onclick = async () => {
    clearFlash('settings-msg');
    try {
      await api('/api/auth/password', {
        method: 'POST',
        body: { current_password: $('s-current').value, new_password: $('s-new').value },
      });
      $('s-current').value = $('s-new').value = '';
      flash('settings-msg', 'Password changed.', 'ok');
    } catch (err) { flash('settings-msg', err.message); }
  };

  for (const btn of document.querySelectorAll('[data-close]'))
    btn.onclick = () => closeModal(btn.dataset.close);
  for (const overlay of document.querySelectorAll('.overlay'))
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.classList.add('hidden'); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape')
      for (const overlay of document.querySelectorAll('.overlay:not(.hidden)'))
        overlay.classList.add('hidden');
  });
}

async function boot() {
  bind();
  applyPreset('public');
  await loadBranding();
  try {
    const user = await api('/api/auth/me');
    showApp(user);
  } catch (_) {
    showLogin();
  }
}

document.addEventListener('DOMContentLoaded', boot);
