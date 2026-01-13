/* ---------------------------------------------------------------------------
 * app.js
 *
 * Main single-page application logic for Project Forms.
 *
 * Responsibilities:
 * - Fetch current user (/api/me) and build the main/admin tab layout.
 * - Load and filter projects (/api/projects) and manage project selection.
 * - Render the merged form definition for a project (/api/projects/{id}/form),
 *   collect answers, and submit records (/api/projects/{id}/records).
 * - Display previous records and record detail views (/api/records/{id}).
 * - Render numeric trends using Chart.js, with an accessibility-first legend
 *   (dash/point styles) and an optional high-contrast colour palette.
 * - Provide admin tools for user management, question set management, and
 *   project configuration (question sets + bespoke/custom questions).
 *
 * Notes:
 * - This file runs in the browser without a bundler; it relies on helpers
 *   exported by common.js (apiGet/apiPost/... and el/showModal).
 * - The code is organized into sections (state, init, main tab, chart, admin).
 * ------------------------------------------------------------------------- */
/* eslint-env browser */

(function () {
  'use strict';

let ME = null;
let PROJECTS = [];
let LAST_MY_ANSWERS = {};
let CURRENT_PROJECT_ID = null;
let FORM = null;
let QUESTIONS = {}; // id -> {text,type,required}
let CHART = null;
let ADMIN_SHOW_DELETED = false; // Projects tab: show soft-deleted projects

// Numeric trend chart colour toggle.
// Default is monochrome (black) to avoid relying on colour for meaning.
const NUMERIC_CHART_BLACK = '#111827';
const NUMERIC_CHART_COLOUR_STORAGE_KEY = 'numericChartColourEnabled';
// High-contrast, colour-blind-friendly, dark-ish palette on bright backgrounds.
const NUMERIC_CHART_PALETTE = [
  '#005A9C', // blue
  '#B00020', // dark red
  '#0B6E4F', // green
  '#6A0DAD', // purple
  '#C15C00', // dark orange
  '#006A8E', // teal
  '#8A2D3C', // maroon
  '#2F4F4F'  // dark slate
];

function getNumericChartColourEnabled() {
  try { return localStorage.getItem(NUMERIC_CHART_COLOUR_STORAGE_KEY) === '1'; } catch { return false; }
}

// Backwards-compatible alias (older code paths may call this name)
function isNumericChartColourEnabled() {
  return getNumericChartColourEnabled();
}

// Palette selector used by the numeric trends chart.
// When colour is disabled we return a single black entry so all series render black.
function getNumericChartPalette() {
  return getNumericChartColourEnabled() ? NUMERIC_CHART_PALETTE : [NUMERIC_CHART_BLACK];
}

function setNumericChartColourEnabled(enabled) {
  try { localStorage.setItem(NUMERIC_CHART_COLOUR_STORAGE_KEY, enabled ? '1' : '0'); } catch {}
}

function applyNumericChartColourMode() {
  if (!CHART) return;
  const enabled = getNumericChartColourEnabled();
  CHART.data.datasets.forEach((ds, i) => {
    const c = enabled ? NUMERIC_CHART_PALETTE[i % NUMERIC_CHART_PALETTE.length] : NUMERIC_CHART_BLACK;
    ds.borderColor = c;
    ds.pointBackgroundColor = c;
    ds.pointBorderColor = c;
  });
  CHART.update();
}

function bindNumericChartColourToggle() {
  // The checkbox is rendered in app.html (id="numericChartColour").
  // Older builds used id="numericChartColourToggle"; support both.
  const cb = document.getElementById('numericChartColour') || document.getElementById('numericChartColourToggle');
  if (!cb) return;

  cb.checked = getNumericChartColourEnabled();
  cb.addEventListener('change', () => {
    setNumericChartColourEnabled(cb.checked);
    // Apply immediately to current chart (and persist for next loads)
    applyNumericChartColourMode();
  });
}

const tabsEl = document.getElementById('tabs');
const mainView = document.getElementById('mainView');
const adminView = document.getElementById('adminView');
const adminContent = document.getElementById('adminContent');

function setTab(name) {
  [...tabsEl.children].forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  if (name === 'main') {
    mainView.style.display = '';
    adminView.style.display = 'none';
  } else {
    mainView.style.display = 'none';
    adminView.style.display = '';
    renderAdmin(name);
  }
}

function buildTabs() {
  tabsEl.innerHTML = '';
  tabsEl.appendChild(el('div', {class:'tab active', 'data-tab':'main', onclick: () => setTab('main')}, 'Main'));
  if (ME && ME.is_admin) {
    tabsEl.appendChild(el('div', {class:'tab', 'data-tab':'users', onclick: () => setTab('users')}, 'User Management'));
    tabsEl.appendChild(el('div', {class:'tab', 'data-tab':'survey', onclick: () => setTab('survey')}, 'Survey'));
    tabsEl.appendChild(el('div', {class:'tab', 'data-tab':'projects', onclick: () => setTab('projects')}, 'Projects'));
  }
}

async function init() {
  try {
    ME = await apiGet('/api/me');
  } catch (e) {
    location.href = '/index.html';
    return;
  }
  document.getElementById('who').textContent = `Signed in as ${ME.email}${ME.is_admin ? ' (admin)' : ''}`;
  buildTabs();
  bindNumericChartColourToggle();

  document.getElementById('logoutBtn').addEventListener('click', async () => {
    try { await apiPost('/api/logout', {}); } catch {}
    location.href = '/index.html';
  });

  document.getElementById('apiTokenBtn').addEventListener('click', async () => {
    try {
      const data = await apiPost('/api/me/api_token/regenerate', {});
      showModal('API token', el('div', {},
        el('p', {}, 'Copy and store this token now. It will not be shown again unless you regenerate it.'),
        el('input', {value: data.api_token, readonly:true, style:'width:100%;'},),
        el('p', {class:'small'}, 'Use: Authorization: Bearer <token> (also works in this browser session).')
      ));
    } catch (e) {
      alert(e.message);
    }
  });

  await loadProjects();
}

async function loadProjects() {
  PROJECTS = await apiGet('/api/projects');
  const sel = document.getElementById('projectSelect');
  const search = document.getElementById('projectSearch');

  function renderOptions(filterText) {
    const f = (filterText || '').toLowerCase().trim();
    const prev = sel.value;
    sel.innerHTML = '';

    const items = PROJECTS.filter(p => !f || (p.name || '').toLowerCase().includes(f) || String(p.focalpoint_code || '').includes(f));

    for (const p of items) {
      const label = (p.focalpoint_code !== null && p.focalpoint_code !== undefined)
        ? `${p.name}  [${p.focalpoint_code}]`
        : p.name;
      sel.appendChild(el('option', {value: String(p.id)}, label));
    }
    sel.appendChild(el('option', {value: '__new__'}, 'New project...'));

    // try to preserve selection
    if (prev && Array.from(sel.options).some(o => o.value === prev)) {
      sel.value = prev;
    }
  }

  renderOptions(search ? search.value : '');

  if (search && !search._bound) {
    search._bound = true;
    search.addEventListener('input', () => renderOptions(search.value));
  }

  if (!sel._bound) {
    sel._bound = true;
    sel.addEventListener('change', async () => {
      if (sel.value === '__new__') {
        const nameInp = el('input', {type:'text', placeholder:'Project name', style:'width:100%;'});
        const fpInp = el('input', {type:'number', placeholder:'Focalpoint code', style:'width:100%;', inputmode:'numeric'});
        showModal('Create new project', el('div', {},
          el('p', {}, 'Create a new project.'),
          el('div', {class:'small'}, 'Project Name'), nameInp,
          el('div', {class:'small', style:'margin-top:8px;'}, 'Focalpoint Code'), fpInp,
          el('p', {class:'small', style:'margin-top:8px;'}, 'The new project will appear in your list.')
        ), async () => {
          const name = nameInp.value.trim();
          const fp = parseInt(fpInp.value, 10);
          if (!name) return;
          if (!Number.isFinite(fp)) { alert('Please enter a focalpoint code.'); return; }
          await apiPost('/api/projects', {name, focalpoint_code: fp});
          await loadProjects();
          // select new
          const newP = PROJECTS.find(x => x.name === name);
          if (newP) {
            sel.value = String(newP.id);
            await selectProject(newP.id);
          }
        });
        // reset dropdown to previous valid selection if any
        const first = Array.from(sel.options).find(o => o.value !== '__new__');
        if (first) sel.value = first.value;
        return;
      }
      await selectProject(parseInt(sel.value, 10));
    });
  }

  if (PROJECTS.length > 0) {
    // ensure a valid selection
    const first = Array.from(sel.options).find(o => o.value !== '__new__');
    if (first) {
      sel.value = first.value;
      await selectProject(parseInt(sel.value, 10));
    }
  } else {
    document.getElementById('formHint').textContent = 'No projects available for your account.';
  }
}



async function loadRememberAnswers(projectId){
  LAST_MY_ANSWERS = {};
  try{
    const rec = await apiGet(`/api/projects/${projectId}/last_record?mine=true`);
    if(rec && rec.answers){ LAST_MY_ANSWERS = rec.answers; }
  }catch(e){
    // ignore; remember is best-effort
    LAST_MY_ANSWERS = {};
  }
}

async function selectProject(projectId) {
  CURRENT_PROJECT_ID = projectId;
  document.getElementById('formHint').textContent = '';
  await loadRememberAnswers(projectId);
  await loadForm();
  await loadRecords();
  await loadChart();
}


function applyRememberValue(q, inputEl){
  if(!q || !q.remember) return;
  if(!LAST_MY_ANSWERS) return;
  const id = q.id;
  if(!Object.prototype.hasOwnProperty.call(LAST_MY_ANSWERS, id)) return;
  const v = LAST_MY_ANSWERS[id];
  if(v === null || v === undefined || v === "") return;

  const t = q.type;
  if(t === "yes_no"){
    // stored may be 'yes'/'no', boolean, or 0/1
    const yes = (v === true) || (v === 1) || (v === "1") || (String(v).toLowerCase() === "yes") || (String(v).toLowerCase() === "true");
    const target = inputEl.querySelector(`input[type="radio"][value="${yes ? "yes" : "no"}"]`);
    if(target) target.checked = true;
    return;
  }
  if(inputEl.tagName === "SELECT"){
    inputEl.value = String(v);
    return;
  }
  // text/textarea/number/date
  inputEl.value = String(v);
}

function renderField(q) {
  const id = q.id;
  const type = q.type || 'short_text';
  const auto = q.auto || null;
  const proj = (PROJECTS || []).find(pp => String(pp.id) === String(CURRENT_PROJECT_ID));
  let input;

  if (type === 'long_text') {
    input = el('textarea', {'data-qid': id});
  } else if (type === 'integer') {
    input = el('input', {type:'number', step:'1', 'data-qid': id});
  } else if (type === 'float') {
    input = el('input', {type:'number', step:'any', 'data-qid': id});
  } else if (type === 'date') {
    input = el('input', {type:'date', 'data-qid': id});
  } else if (type === 'dropdown') {
    const opts = (q.options || []);
    input = el('select', {'data-qid': id},
      el('option', {value:''}, '— select —'),
      ...opts.map(o => el('option', {value:String(o)}, String(o)))
    );
  } else if (type === 'dropdown_mapped') {
    const opts = (q.options && q.options.length) ? q.options : Object.keys(q.value_map || {});
    const vm = q.value_map || {};
    input = el('select', {'data-qid': id},
      el('option', {value:''}, '— select —'),
      ...opts.map(label => {
        const v = (vm && Object.prototype.hasOwnProperty.call(vm, label)) ? vm[label] : label;
        return el('option', {value:String(v)}, String(label));
      })
    );
  } else if (type === 'yes_no') {
    // store "yes"/"no"
    input = el('div', {},
      el('label', {class:'row', style:'gap:6px; align-items:center; margin-right:10px; display:inline-flex;'},
        el('input', {type:'radio', name:`q_${id}`, value:'yes', 'data-qid': id}),
        el('span', {class:'small'}, 'Yes')
      ),
      el('label', {class:'row', style:'gap:6px; align-items:center; display:inline-flex;'},
        el('input', {type:'radio', name:`q_${id}`, value:'no', 'data-qid': id}),
        el('span', {class:'small'}, 'No')
      )
    );
  } else {
    // short_text default
    input = el('input', {type:'text', 'data-qid': id});
  }

  // Apply auto-fill behavior (project name / focalpoint code)
  if (auto && proj && auto.source === 'project_name') {
    if (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA') {
      input.value = proj.name || '';
      input.readOnly = true;
    }
  }
  if (auto && proj && auto.source === 'project_focalpoint_code') {
    if (input.tagName === 'INPUT') {
      input.value = (proj.focalpoint_code ?? '');
      input.readOnly = true;
    }
  }

  applyRememberValue(q, input);

  const label = el('div', {class:'small' + (q.required ? ' required' : '')},
    q.text,
    q.required
      ? el('span', {class:'req', 'aria-hidden':'true'}, ' *')
      : el('span', {class:'small'}, ' (optional)')
  );
  return el('div', {class:'field'}, label, input);
}


async function loadForm() {
  FORM = await apiGet(`/api/projects/${CURRENT_PROJECT_ID}/form`);
  const cont = document.getElementById('formContainer');
  cont.innerHTML = '';
  QUESTIONS = {};
  for (const sec of (FORM.sections || [])) {
    const secNode = el('div', {class:'form-section'},
      el('h3', {}, sec.title || 'Section'),
    );
    for (const q of (sec.questions || [])) {
      QUESTIONS[q.id] = {text:q.text, type:q.type, required:!!q.required, options:q.options, value_map:q.value_map};
      secNode.appendChild(renderField(q));
    }
    cont.appendChild(secNode);
  }
  const submitBtn = el('button', {style:'margin-top:10px;'}, 'Submit record');
  const msg = el('p', {class:'small', id:'submitMsg'});
  submitBtn.addEventListener('click', async () => {
    msg.textContent = '';
    msg.className = 'small';
    try {
      const answers = {};
      const seen = new Set();
      cont.querySelectorAll('[data-qid]').forEach(inp => {
        const qid = inp.getAttribute('data-qid');
        const q = QUESTIONS[qid] || {};
        if (inp.type === 'radio') {
          if (!inp.checked) return;
        }
        // For radio groups, only use checked one
        let v = inp.value;
        if (v === '') v = null;

        // Coerce numeric types / mapped dropdowns
        if (v !== null) {
          if (q.type === 'integer' || q.type === 'dropdown_mapped') {
            const n = parseInt(String(v), 10);
            v = Number.isFinite(n) ? n : null;
          } else if (q.type === 'float') {
            const f = parseFloat(String(v));
            v = Number.isFinite(f) ? f : null;
          }
        }
        answers[qid] = v;
        seen.add(qid);
      });

      await apiPost(`/api/projects/${CURRENT_PROJECT_ID}/records`, {answers});
      msg.textContent = 'Saved.';
      msg.className = 'small ok';
      // clear inputs
      cont.querySelectorAll('[data-qid]').forEach(inp => {
        if (inp.type === 'radio') inp.checked = false;
        else if (inp.tagName === 'SELECT') inp.value = '';
        else inp.value = '';
      });
      await loadRecords();
      await loadChart();
    } catch (e) {
      msg.textContent = e.message || 'Submit failed';
      msg.className = 'small error';
    }
  });
  cont.appendChild(submitBtn);
  cont.appendChild(msg);
}

async function loadRecords() {
  const list = await apiGet(`/api/projects/${CURRENT_PROJECT_ID}/records`);
  const listEl = document.getElementById('recordList');
  const detailEl = document.getElementById('recordDetail');
  listEl.innerHTML = '';
  detailEl.innerHTML = '<p class="small">Select a record.</p>';

  let activeId = null;
  for (const item of list) {
    const node = el('div', {class:'list-item', onclick: async () => {
      activeId = item.id;
      [...listEl.children].forEach(x => x.classList.toggle('active', parseInt(x.dataset.id,10) === activeId));
      await showRecord(item.id);
    }, 'data-id': String(item.id)}, item.created_at);
    listEl.appendChild(node);
  }
  if (list.length > 0) {
    // auto select first
    listEl.children[0].click();
  }
}

async function showRecord(recordId) {
  const detailEl = document.getElementById('recordDetail');
  detailEl.innerHTML = '';
  const rec = await apiGet(`/api/records/${recordId}`);

  const header = el('div', {class:'row wrap', style:'justify-content:space-between; align-items:flex-start;'},
    el('div', {},
      el('p', {class:'small'}, `Record: ${rec.created_at}`),
      el('p', {class:'small'}, `Status: ${rec.review_status || 'pending'}${rec.updated_at ? ' • updated ' + rec.updated_at : ''}`),
      rec.review_comment ? el('p', {class:'small'}, `Comment: ${rec.review_comment}`) : el('span', {})
    ),
    el('div', {class:'row'},
      el('button', {type:'button', onclick: async () => {
        // Edit answers as JSON, but show human-readable question text alongside the UUID.
        // Format: { "Question text [uuid]": <value>, ... }
        const answers = rec.answers || {};

        const labelToId = {};
        for (const [qid, q] of Object.entries(rec.questions || {})) {
          const t = (q && q.text) ? String(q.text) : '';
          if (!t) continue;
          // Only map if unique to avoid accidental collisions.
          if (labelToId[t] === undefined) labelToId[t] = qid;
          else labelToId[t] = null;
        }

        const displayObj = {};
        for (const [qid, v] of Object.entries(answers)) {
          const q = (rec.questions && rec.questions[qid]) || QUESTIONS[qid] || null;
          const label = (q && q.text) ? String(q.text) : qid;
          const key = label === qid ? qid : `${label} [${qid}]`;
          displayObj[key] = v;
        }

        const ta = el('textarea', {}, JSON.stringify(displayObj, null, 2));
        showModal('Edit record (JSON)', el('div', {},
          el('p', {class:'small'}, 'Edit the answers JSON. Keys include the question text plus the UUID in brackets. Saving resets review status to pending.'),
          ta
        ), async () => {
          const parsed = JSON.parse(ta.value);

          // Accept multiple input styles:
          // 1) {"uuid": value}
          // 2) {"Question text [uuid]": value}
          // 3) {"Question text": value} (only if text maps uniquely)
          // 4) [{id:"uuid", value: ...}, ...]
          const out = {};
          const uuidRe = /\[([0-9a-fA-F-]{36})\]\s*$/;

          if (Array.isArray(parsed)) {
            for (const row of parsed) {
              if (!row || typeof row !== 'object') continue;
              const id = row.id || row.qid || row.question_id;
              if (typeof id === 'string' && id.length) out[id] = row.value;
            }
          } else if (parsed && typeof parsed === 'object') {
            for (const [k, v] of Object.entries(parsed)) {
              if (typeof k !== 'string') continue;
              const key = k.trim();
              if (/^[0-9a-fA-F-]{36}$/.test(key)) {
                out[key] = v;
                continue;
              }
              const m = key.match(uuidRe);
              if (m && m[1]) {
                out[m[1]] = v;
                continue;
              }
              const mapped = labelToId[key];
              if (mapped && typeof mapped === 'string') out[mapped] = v;
            }
          }

          await apiPut(`/api/records/${recordId}`, {answers: out});
          await showRecord(recordId);
          await loadRecords();
          await loadChart();
        });
      }}, 'Edit')
    )
  );

  const kvs = el('div', {class:'kv'});

  // Order answers to match the current Form panel question order.
  // Any answers that don't match a current form field are pushed to the bottom.
  const orderMap = new Map();
  const form = FORM;
  let idx = 0;
  if (form && Array.isArray(form.sections)) {
    for (const sec of form.sections) {
      if (!sec || !Array.isArray(sec.questions)) continue;
      for (const q of sec.questions) {
        const id = q && q.id ? String(q.id) : null;
        if (!id) continue;
        if (!orderMap.has(id)) orderMap.set(id, idx++);
      }
    }
  } else if (form && Array.isArray(form.fields)) {
    for (const f of form.fields) {
      const id = f && f.id ? String(f.id) : null;
      if (!id) continue;
      if (!orderMap.has(id)) orderMap.set(id, idx++);
    }
  }

  const entries = Object.entries(rec.answers || {}).map(([qid, v]) => {
    const q = rec.questions[qid] || QUESTIONS[qid] || {text: qid};
    const label = q && q.text ? String(q.text) : String(qid);
    const ord = orderMap.has(String(qid)) ? orderMap.get(String(qid)) : 1e9;
    return {qid, v, q, label, ord};
  });

  entries.sort((a, b) => {
    if (a.ord !== b.ord) return a.ord - b.ord;
    return a.label.localeCompare(b.label);
  });

  for (const {qid, v, q} of entries) {
    kvs.appendChild(el('div', {class:'k'}, q.text));
    let disp = '';
    if (v !== null && v !== undefined) {
      if (q.type === 'dropdown_mapped' && q.value_map) {
        const inv = {};
        for (const [k, num] of Object.entries(q.value_map)) inv[String(num)] = k;
        disp = inv[String(v)] !== undefined ? inv[String(v)] : String(v);
      } else if (q.type === 'yes_no') {
        const s = String(v).toLowerCase();
        disp = (s === 'true' || s === 'yes' || s === '1') ? 'Yes' : (s === 'false' || s === 'no' || s === '0') ? 'No' : String(v);
      } else {
        disp = String(v);
      }
    }
    kvs.appendChild(el('div', {class:'v'}, disp));
  }

  detailEl.appendChild(header);

  // Admin review controls
  if (ME && ME.is_admin) {
    const comment = el('input', {type:'text', placeholder:'Review comment (optional)', style:'flex:1; min-width:240px;'});
    const approveBtn = el('button', {type:'button', onclick: async () => {
      await apiPost(`/api/records/${recordId}/review`, {status:'approved', comment: comment.value.trim() || null});
      await showRecord(recordId);
    }}, 'Approve');
    const rejectBtn = el('button', {class:'danger', onclick: async () => {
      await apiPost(`/api/records/${recordId}/review`, {status:'rejected', comment: comment.value.trim() || null});
      await showRecord(recordId);
    }}, 'Reject');

    detailEl.appendChild(el('div', {class:'row wrap', style:'margin:10px 0;'}, comment, approveBtn, rejectBtn));
  }

  detailEl.appendChild(kvs);
}

async function loadChart() {
  const data = await apiGet(`/api/projects/${CURRENT_PROJECT_ID}/records?include_answers=1&limit=200`);
  // build series per numeric question
  const labels = data.map(r => r.created_at).reverse();
  const series = {}; // label -> array
  const questions = (data[0] && data[0].questions) ? data[0].questions : QUESTIONS;

  const chartableTypes = new Set(['integer','float','dropdown_mapped','yes_no']);
  const toNum = (t, v) => {
    if (v === null || v === undefined || v === '') return null;
    if (t === 'yes_no') {
      if (v === true || v === 1 || v === '1' || (typeof v === 'string' && v.toLowerCase() === 'yes')) return 1;
      if (v === false || v === 0 || v === '0' || (typeof v === 'string' && v.toLowerCase() === 'no')) return 0;
      return null;
    }
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  // initialize numeric keys found
  // We'll scan all records and pick questions whose type is integer/float and any parseable numeric value exists
  const recs = data.slice().reverse();
  for (const r of recs) {
    for (const [qid, val] of Object.entries(r.answers || {})) {
      const q = questions[qid];
      if (!q) continue;
      if (!chartableTypes.has(q.type)) continue;
      if (!series[q.text]) series[q.text] = [];
    }
  }
  for (const [name, arr] of Object.entries(series)) {
    series[name] = new Array(labels.length).fill(null);
  }
  recs.forEach((r, idx) => {
    for (const [qid, val] of Object.entries(r.answers || {})) {
      const q = questions[qid];
      if (!q) continue;
      if (!chartableTypes.has(q.type)) continue;
      const n = toNum(q.type, val);
      if (n !== null) {
        series[q.text][idx] = n;
      }
    }
  });

  const dashPatterns = [
    [], [8,4], [3,3], [12,4,2,4], [2,6], [16,6,4,6], [1,4]
  ];
  const pointStyles = [
    'circle','triangle','rect','rectRounded','cross','crossRot','star','line','dash'
  ];

  // Sort datasets alphabetically so legend order is stable and easy to scan.
  const seriesEntries = Object.entries(series)
    .sort((a, b) => a[0].localeCompare(b[0], undefined, { sensitivity: 'base' }));

  const colourEnabled = isNumericChartColourEnabled();
  const palette = getNumericChartPalette();

  const datasets = seriesEntries.map(([name, values], i) => {
    const c = colourEnabled ? palette[i % palette.length] : NUMERIC_CHART_BLACK;
    return {
    label: name,
    data: values,
    spanGaps: true,
    // Accessibility: rely on line/point style (not colour) to distinguish series
    borderColor: c,
    backgroundColor: 'rgba(0,0,0,0)',
    borderWidth: 2,
    borderDash: dashPatterns[i % dashPatterns.length],
    pointStyle: pointStyles[i % pointStyles.length],
    pointRadius: 3,
    pointHoverRadius: 5,
    pointBackgroundColor: c,
    pointBorderColor: c,
  };
  });

  const ctx = document.getElementById('chart');
  if (CHART) CHART.destroy();
  CHART = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          labels: { usePointStyle: true, boxWidth: 12, color: '#111827' },
          // Legend interactions:
          // - normal click toggles a single series
          // - Shift+click OR quick double-click isolates/un-isolates the clicked series
          onClick: (e, legendItem, legend) => {
            const chart = legend.chart;
            const idx = legendItem.datasetIndex;

            const now = Date.now();
            const last = chart.$lastLegendClick;
            const isDouble = !!last && last.idx === idx && (now - last.t) < 350;
            chart.$lastLegendClick = { idx, t: now };

            const native = e?.native || e;
            const isIsolate = (native && native.shiftKey) || isDouble;

            if (isIsolate) {
              const visibleCount = chart.data.datasets.reduce((acc, _ds, i) => acc + (chart.isDatasetVisible(i) ? 1 : 0), 0);
              const alreadySolo = visibleCount === 1 && chart.isDatasetVisible(idx);
              if (alreadySolo) {
                // restore all
                chart.data.datasets.forEach((_ds, i) => chart.setDatasetVisibility(i, true));
              } else {
                // isolate idx
                chart.data.datasets.forEach((_ds, i) => chart.setDatasetVisibility(i, i === idx));
              }
              chart.update();
              return;
            }

            // Default behavior: toggle the clicked dataset
            chart.setDatasetVisibility(idx, !chart.isDatasetVisible(idx));
            chart.update();
          }
        },
        tooltip: { enabled: true }
      },
      scales: {
        x: { ticks: { maxRotation: 0, autoSkip: true, color:'#111827' }, grid:{ color:'rgba(17,24,39,0.25)' } },
        y: { ticks: { color:'#111827' }, grid:{ color:'rgba(17,24,39,0.25)' } },
      }
    }
  });
}

// ---------------- Admin views ----------------

async function renderAdmin(tabName) {
  if (!ME || !ME.is_admin) return;
  adminContent.innerHTML = '';

  if (tabName === 'users') return await renderAdminUsers();
  if (tabName === 'survey') return await renderAdminSurvey();
  if (tabName === 'projects') return await renderAdminProjects();
}

async function renderAdminUsers() {
  const wrap = el('div', {}, el('h2', {}, 'User Management'));
  const inviteBox = el('div', {class:'card', style:'background:var(--surface-2); margin:12px 0;'},
    el('h3', {}, 'Create invite'),
    el('p', {class:'small'}, 'Optional: set an email to bind the invite to a specific person.'),
  );
  const emailInp = el('input', {type:'email', placeholder:'Invitee email (optional)', style:'flex:1; min-width:240px;'});
  const out = el('div', {class:'small', style:'margin-top:8px;'});
  inviteBox.appendChild(el('div', {class:'row wrap'},
    emailInp,
    el('button', {type:'button', onclick: async () => {
      out.textContent = '';
      try {
        const resp = await apiPost('/api/admin/invites', {email: emailInp.value.trim() || null});
        out.innerHTML = '';
        out.appendChild(el('div', {}, el('b', {}, 'Link: '), el('span', {}, resp.link)));
        out.appendChild(el('div', {}, el('b', {}, 'Secret: '), el('span', {}, resp.secret)));
        out.appendChild(el('div', {class:'small'}, 'Share the link and the secret separately.'));
      } catch(e) {
        out.textContent = e.message;
        out.className = 'small error';
      }
    }}, 'Create invite')
  ));
  inviteBox.appendChild(out);
  wrap.appendChild(inviteBox);

  const users = await apiGet('/api/admin/users');
  const projects = await apiGet('/api/admin/projects' + (ADMIN_SHOW_DELETED ? '?include_deleted=1' : ''));

  const table = el('div', {class:'card', style:'background:var(--surface-2);'},
    el('h3', {}, 'Users'),
    el('p', {class:'small'}, 'Role and project access rules: if a user has assigned projects, they only see those. If they have banned projects, they never see those. If assigned and banned are empty, they see everything.'),
  );

  for (const u of users) {
    const assignedSel = el('select', {multiple:true, size: Math.min(6, projects.length), style:'min-width:220px;'});
    const bannedSel = el('select', {multiple:true, size: Math.min(6, projects.length), style:'min-width:220px;'});
    for (const p of projects) {
      const o1 = el('option', {value: String(p.id)}, p.name);
      const o2 = el('option', {value: String(p.id)}, p.name);
      if (u.assigned_project_ids.includes(p.id)) o1.selected = true;
      if (u.banned_project_ids.includes(p.id)) o2.selected = true;
      assignedSel.appendChild(o1);
      bannedSel.appendChild(o2);
    }

    const adminChk = el('input', {type:'checkbox'});
    adminChk.checked = !!u.is_admin;

    table.appendChild(el('div', {class:'card', style:'background:var(--surface-2); margin:10px 0;'},
      el('div', {class:'row wrap', style:'justify-content:space-between;'},
        el('div', {}, el('b', {}, u.email)),
        el('div', {class:'row'}, el('span', {class:'small'}, 'Admin'), adminChk),
        el('button', {type:'button', onclick: async () => {
          const assigned = [...assignedSel.selectedOptions].map(o => parseInt(o.value,10));
          const banned = [...bannedSel.selectedOptions].map(o => parseInt(o.value,10));
          try {
            await apiPatch(`/api/admin/users/${u.id}`, {is_admin: adminChk.checked});
            await apiPut(`/api/admin/users/${u.id}/projects`, {assigned_project_ids: assigned, banned_project_ids: banned});
            alert('Saved.');
          } catch (e) {
            alert(e.message);
          }
        }}, 'Save')
      ),
      el('div', {class:'row wrap', style:'margin-top:10px; align-items:flex-start;'},
        el('div', {}, el('div', {class:'small'}, 'Assigned projects'), assignedSel),
        el('div', {}, el('div', {class:'small'}, 'Banned projects'), bannedSel)
      )
    ));
  }

  wrap.appendChild(table);
  adminContent.appendChild(wrap);
}

async function renderAdminSurvey() {
  const wrap = el('div', {}, el('h2', {}, 'Survey (Question Sets)'));
  const qsets = await apiGet('/api/admin/question_sets');


  const importBox = el('div', {class:'card', style:'background:var(--surface-2); margin:12px 0;'},
    el('h3', {}, 'Import / Create question set'),
    el('p', {class:'small'}, 'Paste JSON below. Supports canonical format or legacy format described in requirements.'),
  );
  const nameInp = el('input', {type:'text', placeholder:'Question set name', style:'flex:1; min-width:240px;'});
  const ta = el('textarea', {placeholder:'JSON...'});
  const msg = el('p', {class:'small'});
  importBox.appendChild(el('div', {class:'row wrap'}, nameInp));
  importBox.appendChild(ta);
  importBox.appendChild(el('div', {class:'row', style:'justify-content:flex-end; margin-top:10px;'},
    el('button', {type:'button', onclick: async () => {
      msg.textContent = '';
      try {
        const data = JSON.parse(ta.value);
        const name = nameInp.value.trim() || 'Imported';
        await apiPost('/api/admin/question_sets', {name, data});
        msg.textContent = 'Saved.';
        msg.className = 'small ok';
        await renderAdmin();
        setTab('survey');
      } catch (e) {
        msg.textContent = e.message;
        msg.className = 'small error';
      }
    }}, 'Save as new version')
  ));
  importBox.appendChild(msg);
  wrap.appendChild(importBox);

  const listBox = el('div', {class:'card', style:'background:var(--surface-2);'},
    el('h3', {}, 'Existing question sets (all versions)'),
    el('p', {class:'small'}, 'Editing a set creates a new version under the same name.')
  );

  for (const qs of qsets) {
    const editBtn = el('button', {type:'button', onclick: async () => {
      const text = JSON.stringify(qs.data, null, 2);
      const body = el('div', {}, 
        el('p', {class:'small'}, `Editing: ${qs.name} (creates a new version)`),
        el('textarea', {id:'editTa'}, text)
      );
      showModal('Edit question set', body, async () => {
        const edited = JSON.parse(body.querySelector('textarea').value);
        await apiPut(`/api/admin/question_sets/${qs.id}`, {name: qs.name, data: edited});
        await renderAdmin();
        setTab('survey');
      });
    }}, 'Edit');

    const delBtn = el('button', {class:'danger', onclick: async () => {
      if (!confirm('Delete this question set version? This will also remove it from any projects.')) return;
      await apiDelete(`/api/admin/question_sets/${qs.id}`);
      await renderAdmin();
      setTab('survey');
    }}, 'Delete');

    const btnRow = el('div', {class:'row'}, editBtn, delBtn);

    listBox.appendChild(el('div', {class:'row wrap', style:'justify-content:space-between; align-items:center; border-top:1px solid var(--border); padding-top:10px; margin-top:10px;'},
      el('div', {}, el('b', {}, qs.name), el('div', {class:'small'}, `v@ ${qs.created_at}`)),
      btnRow
    ));
  }

  wrap.appendChild(listBox);
  adminContent.appendChild(wrap);
}

async function renderAdminProjects() {
  const wrap = el('div', {}, el('h2', {}, 'Projects'));
  const projects = await apiGet('/api/admin/projects' + (ADMIN_SHOW_DELETED ? '?include_deleted=1' : ''));
  const qsets = await apiGet('/api/admin/question_sets');
  const assignedMap = await apiPost('/api/admin/projects/question_sets_batch', { project_ids: projects.map(p => p.id) });

  // --- Add / import ---
  const addBox = el('div', {class:'card', style:'background:var(--surface-2); margin:12px 0;'},
    el('h3', {}, 'Add / import projects')
  );
  const nameInp = el('input', {type:'text', placeholder:'New project name', style:'flex:1; min-width:240px;'});
  const fpInp = el('input', {type:'number', placeholder:'Focalpoint code', style:'width:180px;'});
  addBox.appendChild(el('div', {class:'row wrap'},
    nameInp,
    fpInp,
    el('button', {type:'button', onclick: async () => {
      const name = nameInp.value.trim();
      if (!name) return;
      const fp = parseInt(fpInp.value, 10);
      if (!Number.isFinite(fp)) { alert('Please enter a focalpoint code.'); return; }
      await apiPost('/api/projects', {name, focalpoint_code: fp});
      await loadProjects();
      setTab('projects');
    }}, 'Add project')
  ));

  const importTa = el('textarea', {placeholder:'Paste JSON: {"projects":["A","B"]}'});
  addBox.appendChild(importTa);
  addBox.appendChild(el('div', {class:'row', style:'justify-content:flex-end; margin-top:10px;'},
    el('button', {type:'button', onclick: async () => {
      const obj = JSON.parse(importTa.value);
      await apiPut('/api/admin/projects/import', obj);
      await loadProjects();
      setTab('projects');
    }}, 'Import')
  ));
  wrap.appendChild(addBox);

  // --- List + search ---
  const listBox = el('div', {class:'card', style:'background:var(--surface-2);'},
    el('h3', {}, 'Existing projects'),
    el('p', {class:'small'}, 'Assign question sets (versions) and reorder by moving items up/down.')
  );

  const searchInp = el('input', {type:'text', placeholder:'Search projects...', style:'width:100%; max-width:520px;'});
const showDelChk = el('input', {type:'checkbox'});
showDelChk.checked = ADMIN_SHOW_DELETED;
showDelChk.addEventListener('change', async () => {
  ADMIN_SHOW_DELETED = !!showDelChk.checked;
  adminContent.innerHTML = '';
  await renderAdminProjects();
});

listBox.appendChild(el('div', {class:'row wrap', style:'margin:10px 0; gap:14px; align-items:center; justify-content:space-between;'},
  el('div', {style:'flex:1; min-width:260px;'}, searchInp),
  el('label', {class:'row', style:'gap:8px; align-items:center; user-select:none;'}, showDelChk, el('span', {class:'small'}, 'Show deleted'))
));

  const cardsWrap = el('div', {});
  listBox.appendChild(cardsWrap);

  function normalize(s) { return (s || '').toLowerCase(); }

  function renderProjectCard(p) {
    const card = el('div', {class:'card', style:'background:var(--surface-2); margin:10px 0;'});
    const closedChk = el('input', {type:'checkbox'});
    closedChk.checked = !!p.closed;

    // question set assignment
    let assignedIds = (assignedMap && assignedMap[p.id]) ? assignedMap[p.id].slice() : [];
    const origClosed = !!p.closed;
    const origAssigned = assignedIds.slice();

    const assignedList = el('div', {});
    function rerenderAssigned() {
      assignedList.innerHTML = '';
      assignedIds.forEach((qid, idx) => {
        const qs = qsets.find(x => x.id === qid);
        assignedList.appendChild(el('div', {class:'row', style:'justify-content:space-between; align-items:center; padding:8px; margin:6px 0; background:var(--surface-2); border-radius:12px;'},
          el('div', {}, el('b', {}, (qs ? qs.name : '?')), el('div', {class:'small'}, qs ? qs.created_at : '')),
          el('div', {class:'row'},
            el('button', {onclick: () => {
              if (idx <= 0) return;
              const t = assignedIds[idx-1];
              assignedIds[idx-1] = assignedIds[idx];
              assignedIds[idx] = t;
              rerenderAssigned();
            }}, 'Up'),
            el('button', {onclick: () => {
              if (idx >= assignedIds.length-1) return;
              const t = assignedIds[idx+1];
              assignedIds[idx+1] = assignedIds[idx];
              assignedIds[idx] = t;
              rerenderAssigned();
            }}, 'Down'),
            el('button', {class:'danger', onclick: () => {
              assignedIds.splice(idx, 1);
              rerenderAssigned();
            }}, 'Remove')
          )
        ));
      });
      if (!assignedIds.length) {
        assignedList.appendChild(el('div', {class:'small'}, 'No question sets assigned.'));
      }
    }
    rerenderAssigned();

    const addSel = el('select', {style:'min-width:260px;'});
    addSel.appendChild(el('option', {value:''}, 'Add question set...'));
    for (const qs of qsets) {
      addSel.appendChild(el('option', {value:String(qs.id)}, `${qs.name} @ ${qs.created_at}`));
    }
    const addBtn = el('button', {onclick: () => {
      const id = parseInt(addSel.value, 10);
      if (!id || assignedIds.includes(id)) return;
      assignedIds.push(id);
      addSel.value = '';
      rerenderAssigned();
    }}, 'Add');

    // bespoke questions list
    const customList = el('div', {class:'small', style:'margin-top:8px;'});
    async function refreshCustomList() {
      try {
        const cq = await apiGet(`/api/admin/projects/${p.id}/custom_questions`);
        const secs = (cq && cq.sections) ? cq.sections : [];
        if (!secs.length) { customList.textContent = 'Bespoke questions: —'; return; }
        customList.innerHTML = '';
        customList.appendChild(el('div', {class:'small'}, 'Bespoke questions:'));
        for (const s of secs) {
          customList.appendChild(el('div', {class:'small', style:'margin-top:6px;'}, `• ${s.title}`));
          for (const q of (s.questions || [])) {
            customList.appendChild(el('div', {class:'row wrap', style:'gap:8px; margin-left:12px; align-items:center;'}, 
              el('div', {class:'small', style:'flex:1; opacity:0.9;'}, `- ${q.text} (${q.type})${q.required ? '' : ' [optional]'}`),
              el('button', {type:'button', onclick: async () => {
                const qText = el('input', {type:'text', value:q.text || '', style:'width:100%;'});
                const qType = el('select', {}, 
                  el('option', {value:'short_text'}, 'short text'),
                  el('option', {value:'long_text'}, 'long text'),
                  el('option', {value:'integer'}, 'integer'),
                  el('option', {value:'float'}, 'float'),
                  el('option', {value:'date'}, 'date'),
                  el('option', {value:'dropdown'}, 'dropdown'),
                  el('option', {value:'dropdown_mapped'}, 'dropdown (mapped)'),
                  el('option', {value:'yes_no'}, 'yes/no'),
                );
                qType.value = q.type || 'short_text';
                const req = el('input', { type: 'checkbox' });
                req.checked = !!q.required;
                const rememberChk = el('input', { type: 'checkbox' });
                rememberChk.checked = !!q.remember;
                const secInp = el('input', {type:'text', value:s.title || 'Custom', style:'width:100%;'});
                const optsTa = el('textarea', {placeholder:'Options (one per line)', style:'margin-top:8px;'});
                const mapTa = el('textarea', {placeholder:'Mapping JSON, e.g. {"red":2,"amber":1,"green":0}', style:'margin-top:8px;'});
                optsTa.value = (q.options || []).join('\n');
                mapTa.value = q.value_map ? JSON.stringify(q.value_map, null, 2) : '';

                function updateVis() {
                  const t = qType.value;
                  if (t === 'dropdown') { optsTa.style.display = 'block'; mapTa.style.display = 'none'; }
                  else if (t === 'dropdown_mapped') { optsTa.style.display = 'block'; mapTa.style.display = 'block'; }
                  else { optsTa.style.display = 'none'; mapTa.style.display = 'none'; }
                }
                qType.addEventListener('change', updateVis);
                updateVis();

                showModal('Edit bespoke project question', el('div', {},
                  el('div', {class:'small'}, 'Question text'), qText,
                  el('div', {class:'small', style:'margin-top:8px;'}, 'Type'), qType,
                  el('div', {class:'row wrap', style:'margin-top:8px; gap:14px;'},
                    el('label', {class:'row', style:'gap:8px; align-items:center;'}, req, el('span', {class:'small'}, 'Required')),
                    el('label', {class:'row', style:'gap:8px; align-items:center;'}, rememberChk, el('span', {class:'small'}, 'Remember last answer'))
                  ),
                  el('div', {class:'small', style:'margin-top:8px;'}, 'Section title'), secInp,
                  optsTa,
                  mapTa,
                ), async () => {
                  const qq = {
                    id: q.id,
                    text: qText.value.trim(),
                    type: qType.value,
                    required: req.checked,
                    remember: rememberChk.checked
                  };
                  if (qq.type === 'dropdown' || qq.type === 'dropdown_mapped') {
                    qq.options = (optsTa.value || '').split('\n').map(x=>x.trim()).filter(Boolean);
                  }
                  if (qq.type === 'dropdown_mapped') {
                    try { qq.value_map = mapTa.value ? JSON.parse(mapTa.value) : {}; } catch(e){ alert('Mapping JSON is invalid.'); return; }
                  }
                  await apiPut(`/api/admin/projects/${p.id}/custom_questions/${q.id}`, {section_title: secInp.value.trim() || 'Custom', question: qq});
                  await refreshCustomList();
                  if (CURRENT_PROJECT_ID === p.id) await loadForm();
                });
              }}, 'Edit'),
              el('button', {class:'danger', type:'button', onclick: async () => {
                if (!q.id) { alert('This bespoke question has no id yet. Try reopening the Projects tab.'); return; }
                if (!confirm('Delete this bespoke question?')) return;
                try {
                  await apiDelete(`/api/admin/projects/${p.id}/custom_questions/${q.id}`);
                  await refreshCustomList();
                  if (CURRENT_PROJECT_ID === p.id) await loadForm();
                } catch (e) {
                  alert(e.message);
                }
              }}, 'Delete')
            ));
          }
        }
      } catch (e) {
        customList.textContent = 'Bespoke questions: (failed to load)';
      }
    }
    // Load bespoke questions once per card.
    // IMPORTANT: Projects-tab search must be local-only. Re-rendering cards on
    // every keypress would refetch bespoke questions repeatedly and can trip
    // the rate limiter.
    refreshCustomList();

    const customBtn = el('button', {type:'button', onclick: async () => {
      const qText = el('input', {type:'text', placeholder:'Question text', style:'width:100%;'});
      const qType = el('select', {},
        el('option', {value:'short_text'}, 'short text'),
        el('option', {value:'long_text'}, 'long text'),
        el('option', {value:'integer'}, 'integer'),
        el('option', {value:'float'}, 'float'),
        el('option', {value:'date'}, 'date'),
        el('option', {value:'dropdown'}, 'dropdown'),
        el('option', {value:'yes_no'}, 'yes/no'),
        el('option', {value:'dropdown_mapped'}, 'dropdown (mapped)'),
      );

      const req = el('input', {type:'checkbox'});
      req.checked = true;

      const rememberChk = el('input', {type:'checkbox'});
      rememberChk.checked = false;

      const sec = el('input', {type:'text', placeholder:'Section title', value:'Custom', style:'width:100%;'});

      // Options/mapping controls (shown when relevant)
      const optsTa = el('textarea', {
        placeholder:'Options (one per line)',
        style:'margin-top:8px; display:none;'
      });
      const mapTa = el('textarea', {
        placeholder:'Mapping JSON, e.g. {"red":2,"amber":1,"green":0}',
        style:'margin-top:8px; display:none;'
      });

      function updateVis() {
        const t = qType.value;
        if (t === 'dropdown') {
          optsTa.style.display = 'block';
          mapTa.style.display = 'none';
        } else if (t === 'dropdown_mapped') {
          optsTa.style.display = 'block';
          mapTa.style.display = 'block';
        } else {
          optsTa.style.display = 'none';
          mapTa.style.display = 'none';
        }
      }

      qType.addEventListener('change', updateVis);
      updateVis();

      showModal('Add bespoke project question', el('div', {},
        el('p', {class:'small'}, `Project: ${p.name}`),
        el('div', {class:'small'}, 'Question text'), qText,
        el('div', {class:'small', style:'margin-top:8px;'}, 'Type'), qType,
        el('div', {class:'row wrap', style:'margin-top:8px; gap:14px;'},
          el('label', {class:'row', style:'gap:8px; align-items:center;'}, req, el('span', {class:'small'}, 'Required')),
          el('label', {class:'row', style:'gap:8px; align-items:center;'}, rememberChk, el('span', {class:'small'}, 'Remember last answer'))
        ),
        el('div', {class:'small', style:'margin-top:8px;'}, 'Section title'), sec,
        optsTa,
        mapTa,
      ), async () => {
        const q = {
          text: qText.value.trim(),
          type: qType.value,
          required: req.checked,
          remember: rememberChk.checked
        };

        if (q.type === 'dropdown' || q.type === 'dropdown_mapped') {
          q.options = (optsTa.value || '').split('\n').map(s => s.trim()).filter(Boolean);
        }
        if (q.type === 'dropdown_mapped') {
          try {
            q.value_map = mapTa.value ? JSON.parse(mapTa.value) : {};
          } catch {
            alert('Mapping JSON is invalid.');
            return;
          }
        }

        await apiPost(`/api/admin/projects/${p.id}/custom_questions`, {
          section_title: sec.value.trim() || 'Custom',
          question: q
        });
        await refreshCustomList();
        if (CURRENT_PROJECT_ID === p.id) {
          await loadForm();
        }
      });
    }}, 'Add bespoke question');

    const saveBtn = el('button', {type:'button', onclick: async () => {
      // save Closed and Question Sets (single Save)
      const changedClosed = (origClosed !== !!closedChk.checked);
      const changedQsets = (origAssigned.length !== assignedIds.length) || origAssigned.some((v, i) => v !== assignedIds[i]);

      if (!changedClosed && !changedQsets) {
        alert('No changes to save.');
        return;
      }
      if (changedClosed) {
        await apiPatch(`/api/admin/projects/${p.id}`, {closed: closedChk.checked});
      }
      if (changedQsets) {
        await apiPut(`/api/admin/projects/${p.id}/question_sets`, {question_set_ids: assignedIds});
      }
      await loadProjects();
      // Reload tab to guarantee we reflect persisted state
      setTab('projects');
    }}, 'Save');

    const delBtn = el('button', {class:'danger', type:'button', onclick: async () => {
  if (p.deleted_at) return;
  if (!confirm(`Delete project "${p.name}"? This will hide it from users.`)) return;
  try {
    await apiDelete(`/api/admin/projects/${p.id}`);
    await loadProjects();
    setTab('projects');
  } catch (e) {
    alert(e.message);
  }
}}, 'Delete project');
if (p.deleted_at) {
  delBtn.disabled = true;
  delBtn.textContent = 'Deleted';
}

card.appendChild(el('div', {class:'row wrap', style:'justify-content:space-between; align-items:flex-start;'},
  el('div', {},
    el('b', {}, p.name),
    el('div', {class:'small'}, `id: ${p.id}`),
    el('div', {class:'small'}, `Focalpoint: ${p.focalpoint_code ?? '—'}`),
    (p.deleted_at ? el('div', {class:'small', style:'margin-top:4px; color:#ff9b9b;'}, `Deleted: ${p.deleted_at}`) : null)
  ),
  el('div', {class:'row wrap', style:'align-items:center; gap:10px;'},
    el('span', {class:'small'}, 'Closed'), closedChk,
    delBtn
  )
));

    card.appendChild(el('div', {class:'small', style:'margin-top:10px;'}, 'Assigned question sets:'));
    card.appendChild(assignedList);
    card.appendChild(el('div', {class:'row wrap', style:'margin-top:10px;'}, addSel, addBtn));
    card.appendChild(customList);
    card.appendChild(el('div', {class:'row', style:'justify-content:space-between; margin-top:10px;'},
      saveBtn,
      customBtn
    ));

    cardsWrap.appendChild(card);
    return card;
  }

  // Render all project cards once. Filtering is done locally by toggling
  // visibility, which avoids repeated API calls (e.g. bespoke questions fetch)
  // on every keypress.
  cardsWrap.innerHTML = '';
  const cardById = new Map();
  for (const p of projects) {
    const card = renderProjectCard(p);
    cardById.set(p.id, {p, card});
  }
  const noMatch = el('div', {class:'small', style:'margin-top:8px;'}, 'No matching projects.');
  cardsWrap.appendChild(noMatch);

  function applyFilter() {
    const term = normalize(searchInp.value).trim();
    let shown = 0;
    for (const {p, card} of cardById.values()) {
      const ok = !term || normalize(p.name).includes(term);
      card.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }
    noMatch.style.display = shown ? 'none' : '';
  }

  searchInp.addEventListener('input', applyFilter);
  applyFilter();

  wrap.appendChild(listBox);
  adminContent.appendChild(wrap);
}

if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', () => { init(); });
} else {
  init();
}
})();
