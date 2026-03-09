let sessions = [];
let selectedId = null;
let resumeOpen = {};   // track which resume panels are open
let promptTexts = {};  // shortId -> user-typed text (persists across renders)
let promptInited = {}; // shortId -> true if we already pre-filled once
let driveTargetTexts = {};  // shortId -> user-typed drive target text
let driveLogOpen = {};  // shortId -> bool
let prevDetailHash = {};  // shortId -> hash of detail content, skip re-render if unchanged

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
function formatAge(m) {
  if (m < 1) return '<1m';
  if (m < 60) return Math.round(m) + 'm';
  if (m < 1440) return Math.round(m/60) + 'h';
  return Math.round(m/1440) + 'd';
}
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

// Simple markdown-ish renderer for resume reports
function renderMd(raw) {
  if (!raw) return '';
  // Escape HTML first
  let s = esc(raw);
  // ## headings
  s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  // **bold**
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // `code`
  s = s.replace(/`([^`]+)`/g, '<code style="background:var(--surface2);padding:0 3px;border-radius:2px">$1</code>');
  // bullet lists
  s = s.replace(/^- (.+)$/gm, '<li>$1</li>');
  s = s.replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>');
  // Lines starting with " (quoted resume prompts) -> blockquote
  s = s.replace(/^&quot;(.+?)&quot;$/gm, '<blockquote>$1</blockquote>');
  // Remaining newlines -> <br> only if not inside tags
  s = s.replace(/\n/g, '<br>');
  // Clean up double <br> after block elements
  s = s.replace(/(<\/h2>|<\/ul>|<\/blockquote>)<br>/g, '$1');
  return s;
}

function renderStats() {
  const w = sessions.filter(s => s.status === 'working').length;
  const wt = sessions.filter(s => s.status === 'waiting').length;
  const st = sessions.filter(s => s.status === 'stalled').length;
  const i = sessions.filter(s => s.status === 'idle').length;
  const dr = sessions.filter(s => s.drive_active).length;
  let html =
    `<span><span class="dot dot-green"></span>${w} working</span>` +
    `<span><span class="dot" style="background:var(--amber)"></span>${wt} waiting</span>` +
    `<span><span class="dot dot-red"></span>${st} stalled</span>` +
    `<span><span class="dot dot-gray"></span>${i} idle</span>`;
  if (dr > 0) html += `<span><span class="dot" style="background:#818cf8"></span>${dr} driving</span>`;
  document.getElementById('stats').innerHTML = html;
}

let hideDead = true;  // default: hide sessions without a live process

function renderList() {
  const el = document.getElementById('session-list');
  if (!sessions.length) {
    el.innerHTML = '<div style="padding:40px 20px;text-align:center;color:var(--text3)">No sessions found</div>';
    return;
  }
  const visible = hideDead ? sessions.filter(s => s.has_process) : sessions;
  const hiddenCount = sessions.length - visible.length;
  let html = visible.map(s => {
    const sel = s.short_id === selectedId ? ' selected' : '';
    const proj = (s.cwd || s.project).replace(/^\/Users\/[^/]+\//, '~/');
    const isDriving = s.drive_active;
    const badgeStatus = isDriving ? 'driving' : s.status;
    const badge = isDriving ? 'driving' : s.status === 'stalled' ? shortStallType(s.stall_type) : s.status;
    const typeIcon = s.process_type === 'tmux' ? ' [tmux]' : s.process_type === 'tty' ? ' [tty]' : '';
    return `<div class="s-row${sel}" onclick="selectSession('${s.short_id}')">
      <span class="badge badge-${badgeStatus}">[${esc(badge)}]</span>
      <div class="s-info">
        <div class="s-project" title="${esc(s.project)}">${esc(proj)}</div>
        <div class="s-meta">${esc(s.slug || s.short_id)}${s.model ? ' · ' + esc(s.model) : ''}${typeIcon}</div>
      </div>
      <div class="s-age">${s.last_activity}<br>${formatAge(s.age_minutes)} ago</div>
    </div>`;
  }).join('');
  if (hiddenCount > 0) {
    html += `<div style="padding:8px 12px;text-align:center;font-size:11px;color:var(--text3);cursor:pointer;border-top:1px solid rgba(71,85,105,.3)" onclick="hideDead=false;renderList()">+ ${hiddenCount} past sessions (click to show)</div>`;
  } else if (!hideDead && sessions.length > visible.length) {
    html += `<div style="padding:8px 12px;text-align:center;font-size:11px;color:var(--text3);cursor:pointer;border-top:1px solid rgba(71,85,105,.3)" onclick="hideDead=true;renderList()">Hide past sessions</div>`;
  }
  el.innerHTML = html;
}

function shortStallType(t) {
  const m = {tool_hung:'tool hung', no_response_after_tool_result:'no reply', stream_interrupted:'interrupted', no_response_after_user:'no reply'};
  return m[t] || t;
}

function selectSession(shortId) {
  // Save current textarea before switching
  savePromptText();
  selectedId = shortId;
  renderList();
  renderDetail();
}

function savePromptText() {
  if (!selectedId) return;
  const ta = document.getElementById('prompt-input');
  if (ta) promptTexts[selectedId] = ta.value;
}

function renderDetail() {
  const el = document.getElementById('detail');
  const s = sessions.find(s => s.short_id === selectedId);
  if (!s) {
    el.innerHTML = '<div class="detail-empty">Select a session to view details</div>';
    return;
  }
  const proj = (s.cwd || s.project).replace(/^\/Users\/[^/]+\//, '~/');
  const badge = s.status === 'stalled' ? shortStallType(s.stall_type) : s.status;
  const tags = [s.model, s.version, Math.round(s.size_kb) + ' KB', s.slug].filter(Boolean);

  let stallBanner = '';
  if (s.status === 'stalled') {
    stallBanner = `<div class="stall-banner">
      <span class="stall-icon">!</span>
      <span class="stall-text"><strong>${esc(s.stall_type)}</strong> &mdash; ${esc(s.stall_description)}</span>
    </div>`;
  }

  let msgHtml = '';
  let lastRole = '';
  for (const m of s.messages) {
    const mtype = m.type || 'text';
    if (mtype === 'tool_use') {
      msgHtml += `<div class="msg-tool"><span class="msg-tool-name">${esc(m.text.split(':')[0])}</span> <span class="msg-tool-detail">${esc(m.text.substring(m.text.indexOf(':')+1).trim())}</span></div>`;
    } else if (mtype === 'tool_result') {
      msgHtml += `<div class="msg-result">${esc(m.text)}</div>`;
    } else {
      const showRole = m.role !== lastRole;
      msgHtml += `<div class="msg">${showRole ? '<div class="msg-role msg-role-' + m.role + '">' + (m.role === 'user' ? 'You' : 'Claude') + '</div>' : ''}<div class="msg-text">${esc(m.text)}</div></div>`;
      lastRole = m.role;
    }
  }
  if (!msgHtml) msgHtml = '<div style="color:var(--text3);font-size:12px;padding:12px 16px">(no messages in tail)</div>';

  let resumeHtml = '';
  if (s.resume_content) {
    const isOpen = resumeOpen[s.short_id] || false;
    resumeHtml = `<div class="d-resume">
      <div class="resume-toggle" onclick="toggleResume('${s.short_id}')">
        <span class="arrow${isOpen ? ' open' : ''}">&#9654;</span> Resume Report
      </div>
      <div class="resume-body${isOpen ? ' open' : ''}">
        <div class="resume-rendered">${renderMd(s.resume_content)}</div>
      </div>
    </div>`;
  }

  const hasResume = s.resume_content && s.resume_content.trim();
  let actionsHtml = `<div class="d-actions">
    ${hasResume ? `<button class="btn" onclick="copyResume('${s.short_id}')">Copy Resume Prompt</button>` : ''}
    <button class="btn btn-blue" id="sum-btn-${s.short_id}" onclick="doSummarize('${s.short_id}')">
      <span class="spinner"></span>
      <span>${hasResume ? 'Re-summarize' : 'Summarize'}</span>
    </button>
  </div>`;

  // Prompt text: use saved user text, or pre-fill from resume (once only)
  let promptVal = '';
  if (promptTexts[s.short_id] !== undefined) {
    promptVal = promptTexts[s.short_id];
  } else if (!promptInited[s.short_id] && s.resume_content) {
    const m = s.resume_content.match(/## RESUME_PROMPT\n([\s\S]*?)(?:\n##|$)/);
    if (m) promptVal = m[1].trim().replace(/^"|"$/g, '');
    promptInited[s.short_id] = true;
    promptTexts[s.short_id] = promptVal;
  }

  const promptHtml = `<div class="d-prompt">
    <h3>Send to Terminal</h3>
    <textarea id="prompt-input" rows="3" placeholder="Type a prompt to send directly to this session's terminal..." oninput="promptTexts['${s.short_id}']=this.value">${esc(promptVal)}</textarea>
    <div class="prompt-actions">
      <button class="btn btn-blue" id="send-btn" onclick="doSend('${s.short_id}')">
        <span class="spinner"></span><span>Send</span>
      </button>
      <button class="btn" onclick="doCopyPrompt('${s.short_id}')">Copy</button>
    </div>
    <div id="send-status" style="display:none;font-size:12px;margin-top:6px;padding:4px 8px;border-radius:4px"></div>
  </div>`;

  // Build a hash of content that matters — skip re-render if unchanged
  const detailHash = s.status + ':' + s.messages.length + ':' + s.drive_state + ':' + s.drive_iteration + ':' + (s.resume_content ? s.resume_content.length : 0) + ':' + (s.messages.length ? s.messages[s.messages.length-1].text : '') + ':' + (resumeOpen[s.short_id]||0) + ':' + (driveLogOpen[s.short_id]||0) + ':' + (projectMemoryOpen[s.project]||0);
  const isNewContent = detailHash !== prevDetailHash[s.short_id];
  if (!isNewContent) return;  // nothing changed, keep scroll position
  prevDetailHash[s.short_id] = detailHash;

  el.innerHTML = `
    <div class="detail-header">
      <span class="badge badge-${s.status}">[${esc(badge)}]</span>
      <div class="d-title">
        <h2>${esc(proj)}</h2>
        <div class="d-sub">${esc(s.session_id)}</div>
      </div>
      <div style="font-size:12px;color:var(--text3)">${s.last_activity} (${formatAge(s.age_minutes)} ago)</div>
    </div>
    <div class="d-tags">${tags.map(t => '<span class="tag">' + esc(String(t)) + '</span>').join('')}</div>
    ${stallBanner}
    <div class="detail-body">
      <div class="d-messages">
        <h3>Recent Messages</h3>
        ${msgHtml}
      </div>
      ${resumeHtml}
    </div>
    ${actionsHtml}
    ${renderDrivePanel(s)}
    ${renderProjectMemoryPanel(s)}
    ${promptHtml}`;
  const msgBox = el.querySelector('.d-messages');
  if (msgBox) msgBox.scrollTop = msgBox.scrollHeight;
}

function toggleResume(shortId) {
  resumeOpen[shortId] = !resumeOpen[shortId];
  renderDetail();
}

function copyResume(shortId) {
  const s = sessions.find(s => s.short_id === shortId);
  if (!s || !s.resume_content) return;
  const m = s.resume_content.match(/## RESUME_PROMPT\n([\s\S]*?)(?:\n##|$)/);
  const text = m ? m[1].trim() : s.resume_content;
  navigator.clipboard.writeText(text).then(() => toast('Resume prompt copied!'));
}

async function doSummarize(shortId) {
  const btn = document.getElementById('sum-btn-' + shortId);
  if (!btn || btn.classList.contains('loading')) return;
  btn.classList.add('loading');
  try {
    const resp = await fetch('/api/summarize/' + shortId, {method: 'POST'});
    const data = await resp.json();
    if (data.ok) { toast('Summary generated!'); await doRefresh(); }
    else toast('Error: ' + (data.error || 'unknown'));
  } catch(e) { toast('Request failed'); }
  finally { btn.classList.remove('loading'); }
}

function showSendStatus(msg, ok, duration) {
  const el = document.getElementById('send-status');
  if (!el) return;
  el.style.display = '';
  el.style.background = ok ? 'rgba(34,197,94,.15)' : 'rgba(239,68,68,.15)';
  el.style.color = ok ? '#4ade80' : '#fca5a5';
  el.textContent = msg;
  setTimeout(() => { el.style.display = 'none'; }, duration || 4000);
}

async function doSend(shortId) {
  const input = document.getElementById('prompt-input');
  const text = input ? input.value.trim() : '';
  if (!text) { toast('Type a prompt first'); return; }
  const btn = document.getElementById('send-btn');
  if (btn) btn.classList.add('loading');
  try {
    const resp = await fetch('/api/send/' + shortId, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text})
    });
    const data = await resp.json();
    if (data.ok) {
      const needsEnter = data.message && data.message.includes('press Enter');
      const isClipboard = data.message && data.message.includes('clipboard');
      showSendStatus(data.message, true, (needsEnter || isClipboard) ? 8000 : 4000);
      toast(needsEnter ? 'Pasted! Press Enter in Terminal' : isClipboard ? 'Copied! Switch to Terminal' : 'Sent!');
      // Clear textarea after successful send
      if (input) input.value = '';
      promptTexts[shortId] = '';
    } else {
      showSendStatus(data.error, false);
      toast(data.error);
    }
  } catch(e) { showSendStatus('Request failed', false); }
  finally { if (btn) btn.classList.remove('loading'); }
}

async function doCopyPrompt(shortId) {
  const input = document.getElementById('prompt-input');
  const text = input ? input.value.trim() : '';
  if (!text) { toast('Type a prompt first'); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied!');
  } catch(e) {
    try {
      await fetch('/api/copy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
      toast('Copied!');
    } catch(e2) { toast('Copy failed'); }
  }
}

function renderDrivePanel(s) {
  const isDriving = s.drive_state === 'driving';
  const isDone = s.drive_state === 'done';
  const isPaused = s.drive_state === 'paused';
  const hasDrive = s.drive_state && s.drive_state !== '';

  let html = '<div class="drive-panel"><h3>Drive Mode</h3>';

  if (!hasDrive || isDone || isPaused) {
    // Show target textarea and start button
    const existingTarget = driveTargetTexts[s.short_id] || (hasDrive ? s.drive_target : '') || '';
    const stateNote = isDone ? '<div style="color:var(--green);font-size:11px;margin-bottom:6px">Previous drive completed.</div>' :
                      isPaused ? '<div style="color:var(--amber);font-size:11px;margin-bottom:6px">Drive paused.</div>' : '';
    html += stateNote;
    html += `<textarea class="drive-target-area" id="drive-target-input" rows="3" placeholder="Describe the target for Claude to work toward..." oninput="driveTargetTexts['${s.short_id}']=this.value">${esc(existingTarget)}</textarea>`;
    html += '<div class="drive-actions">';
    html += `<button class="btn btn-blue" id="drive-start-btn" onclick="doStartDrive('${s.short_id}')"><span class="spinner"></span><span>Start Drive</span></button>`;
    html += '</div>';
  } else if (isDriving) {
    // Show active drive info
    html += `<textarea class="drive-target-area" readonly rows="2">${esc(s.drive_target || '')}</textarea>`;
    html += '<div class="drive-actions">';
    html += `<button class="btn" style="background:#5c1d1d;border-color:#991b1b;color:#fca5a5" onclick="doStopDrive('${s.short_id}')">Stop Drive</button>`;
    html += '<div class="drive-progress">';
    const pct = s.drive_progress_pct || 0;
    html += `<div class="drive-progress-bar"><div class="drive-progress-fill" style="width:${pct}%"></div></div>`;
    html += `<span class="drive-progress-text">${pct}%</span>`;
    html += '</div>';
    html += `<span class="drive-iter">${s.drive_iteration || 0}/${s.drive_max_iterations || 50}</span>`;
    html += '</div>';
  }

  // Memory
  if (hasDrive && s.drive_memory && s.drive_memory.length > 0) {
    html += '<div style="margin-top:8px"><h3 style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Memory</h3>';
    html += '<ul class="drive-memory">';
    for (const m of s.drive_memory) {
      html += `<li>${esc(m)}</li>`;
    }
    html += '</ul></div>';
  }

  // Log (collapsible)
  if (hasDrive && s.drive_log && s.drive_log.length > 0) {
    const isOpen = driveLogOpen[s.short_id] || false;
    html += `<div style="margin-top:8px;cursor:pointer;font-size:11px;color:var(--text3)" onclick="driveLogOpen['${s.short_id}']=!driveLogOpen['${s.short_id}'];renderDetail()">`;
    html += `<span class="arrow${isOpen ? ' open' : ''}" style="font-size:10px;display:inline-block;transition:transform .15s${isOpen ? ';transform:rotate(90deg)' : ''}">&#9654;</span> Drive Log (${s.drive_log.length})`;
    html += '</div>';
    if (isOpen) {
      html += '<div class="drive-log">';
      const logs = [...s.drive_log].reverse();
      for (const entry of logs) {
        const ts = entry.ts ? entry.ts.split('T')[1]?.split('.')[0] || '' : '';
        const action = entry.action || '';
        let cls = 'log-eval';
        if (action === 'inject') cls = 'log-inject';
        else if (action === 'done') cls = 'log-done';
        else if (action === 'blocked' || action === 'inject_failed' || action === 'eval_failed') cls = 'log-error';
        else if (action === 'started' || action === 'stopped') cls = 'log-done';
        let text = `[${ts}] ${action}`;
        if (entry.progress_pct !== undefined) text += ` (${entry.progress_pct}%)`;
        if (entry.reasoning) text += ` — ${entry.reasoning}`;
        if (entry.instruction) text += `\n  → ${entry.instruction}`;
        html += `<div class="drive-log-entry ${cls}">${esc(text)}</div>`;
      }
      html += '</div>';
    }
  }

  html += '</div>';
  return html;
}

async function doStartDrive(shortId) {
  const input = document.getElementById('drive-target-input');
  const target = input ? input.value.trim() : '';
  if (!target) { toast('Enter a target first'); return; }
  const btn = document.getElementById('drive-start-btn');
  if (btn) btn.classList.add('loading');
  try {
    const resp = await fetch('/api/drive/start/' + shortId, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target, check_interval: 30, max_iterations: 50})
    });
    const data = await resp.json();
    if (data.ok) { toast('Drive started!'); await doRefresh(); }
    else toast('Error: ' + (data.error || 'unknown'));
  } catch(e) { toast('Request failed'); }
  finally { if (btn) btn.classList.remove('loading'); }
}

async function doStopDrive(shortId) {
  try {
    const resp = await fetch('/api/drive/stop/' + shortId, {method: 'POST'});
    const data = await resp.json();
    if (data.ok) { toast('Drive stopped'); await doRefresh(); }
    else toast('Error: ' + (data.error || 'unknown'));
  } catch(e) { toast('Request failed'); }
}

// --- Project Memory (structured) ---
let _pmemProject = '';
let _pmemData = {};  // {constraints:[], results:[], decisions:[], working_config:[]}
let projectMemoryOpen = {};
const PMEM_CATS = ['constraints','results','decisions','working_config'];
const PMEM_LABELS = {constraints:'Constraints',results:'Results',decisions:'Decisions',working_config:'Config'};
const PMEM_COLORS = {constraints:'#f87171',results:'#60a5fa',decisions:'#a78bfa',working_config:'#34d399'};

function renderProjectMemoryPanel(s) {
  const proj = s.project || '';
  if (!proj) return '';
  const mem = s.project_memory || {};
  _pmemProject = proj;
  _pmemData = mem;
  const total = PMEM_CATS.reduce((n,c) => n + (mem[c]||[]).length, 0);
  const isOpen = projectMemoryOpen[proj] || false;
  let html = '<div class="drive-panel" style="margin-top:8px">';
  html += '<div style="cursor:pointer" onclick="pmemToggle()">';
  html += '<h3 style="display:flex;align-items:center;gap:6px">';
  html += '<span class="arrow' + (isOpen ? ' open' : '') + '" style="font-size:10px;display:inline-block;transition:transform .15s' + (isOpen ? ';transform:rotate(90deg)' : '') + '">&#9654;</span>';
  html += ' Project Memory (' + total + ')</h3></div>';
  if (isOpen) {
    for (const cat of PMEM_CATS) {
      const items = mem[cat] || [];
      const color = PMEM_COLORS[cat];
      html += '<div style="margin-top:6px"><span style="font-size:10px;font-weight:600;color:' + color + ';text-transform:uppercase;letter-spacing:.5px">' + PMEM_LABELS[cat] + ' (' + items.length + ')</span>';
      html += '<ul class="drive-memory" style="margin:2px 0 0 0">';
      for (let i = 0; i < items.length; i++) {
        html += '<li style="display:flex;justify-content:space-between;align-items:start">';
        html += '<span>' + esc(items[i]) + '</span>';
        html += '<button class="btn" style="padding:0 4px;min-width:auto;font-size:10px;background:transparent;border:none;color:var(--red);cursor:pointer" data-pmem-cat="' + cat + '" data-pmem-idx="' + i + '" onclick="pmemRemove(this)">x</button>';
        html += '</li>';
      }
      html += '</ul></div>';
    }
    html += '<div style="display:flex;gap:4px;margin-top:8px">';
    html += '<select id="pmem-cat" style="background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:4px;border-radius:6px;font-size:11px">';
    for (const cat of PMEM_CATS) html += '<option value="' + cat + '">' + PMEM_LABELS[cat] + '</option>';
    html += '</select>';
    html += '<input id="pmem-input" type="text" placeholder="Add item..." style="flex:1;background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:6px;font-size:12px" onkeydown="if(event.key===\'Enter\')pmemAdd()">';
    html += '<button class="btn btn-blue" style="padding:4px 10px;font-size:11px" onclick="pmemAdd()">Add</button>';
    html += '</div>';
    const sid = (s.session_id || '').substring(0, 8);
    if (sid) {
      html += '<div style="margin-top:6px">';
      html += '<button class="btn" style="padding:4px 10px;font-size:11px;background:var(--bg2);border:1px solid var(--border);color:var(--text)" onclick="pmemSelfSummarize(\'' + sid + '\')">Ask Claude to update memory</button>';
      html += '</div>';
    }
  }
  html += '</div>';
  return html;
}

function pmemToggle() {
  projectMemoryOpen[_pmemProject] = !projectMemoryOpen[_pmemProject];
  renderDetail();
}

async function pmemAdd() {
  const input = document.getElementById('pmem-input');
  const catSel = document.getElementById('pmem-cat');
  if (!input || !input.value.trim() || !catSel) return;
  const cat = catSel.value;
  try {
    const resp = await fetch('/api/project_memory', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project: _pmemProject, category: cat, add: [input.value.trim()]})
    });
    const data = await resp.json();
    if (data.ok) { toast('Added'); await doRefresh(); }
    else toast('Error: ' + (data.error || 'unknown'));
  } catch(e) { toast('Request failed'); }
}

async function pmemRemove(btn) {
  const cat = btn.getAttribute('data-pmem-cat');
  const idx = parseInt(btn.getAttribute('data-pmem-idx'));
  const items = (_pmemData[cat] || []);
  const item = items[idx];
  if (!item) return;
  try {
    const resp = await fetch('/api/project_memory', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project: _pmemProject, category: cat, remove: [item]})
    });
    const data = await resp.json();
    if (data.ok) { toast('Removed'); await doRefresh(); }
    else toast('Error: ' + (data.error || 'unknown'));
  } catch(e) { toast('Request failed'); }
}

async function pmemSelfSummarize(shortId) {
  try {
    const resp = await fetch('/api/memory/summarize/' + shortId, {method: 'POST'});
    const data = await resp.json();
    if (data.ok) toast('Self-summarize triggered');
    else toast('Error: ' + (data.error || 'failed'));
  } catch(e) { toast('Request failed'); }
}

function detailIsBusy() {
  const ta = document.getElementById('prompt-input');
  if (ta && document.activeElement === ta) return true;
  const dta = document.getElementById('drive-target-input');
  if (dta && document.activeElement === dta) return true;
  const pmi = document.getElementById('pmem-input');
  if (pmi && document.activeElement === pmi) return true;
  const btn = document.getElementById('send-btn');
  if (btn && btn.classList.contains('loading')) return true;
  const dbtn = document.getElementById('drive-start-btn');
  if (dbtn && dbtn.classList.contains('loading')) return true;
  return false;
}

async function doRefresh() {
  try {
    savePromptText();
    const resp = await fetch('/api/sessions');
    sessions = await resp.json();
    renderStats();
    renderList();
    if (!selectedId && sessions.length) selectedId = sessions[0].short_id;
    // Skip detail re-render if user is actively using the prompt panel
    if (!detailIsBusy()) renderDetail();
  } catch(e) { console.error('refresh failed', e); }
}

const evtSource = new EventSource('/api/events');
evtSource.onmessage = (e) => {
  savePromptText();
  sessions = JSON.parse(e.data);
  renderStats();
  renderList();
  if (!selectedId && sessions.length) selectedId = sessions[0].short_id;
  if (!detailIsBusy()) renderDetail();
};
