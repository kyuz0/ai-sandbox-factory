(async () => {
  const $ = s => document.querySelector(s);
  const bootstrap = location.hash.slice(1);
  history.replaceState(null, '', location.pathname + location.search);
  if (bootstrap) sessionStorage.setItem('factory-control', bootstrap);
  // Kept in the host-origin page; never sent to the editor or postMessage.
  const token = sessionStorage.getItem('factory-control') || '';
  async function request(path, body) {
    const r = await fetch(path, {method: 'POST', headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    const text = await r.text();
    let value;
    try { value = JSON.parse(text); } catch { throw new Error(`Factory request failed (HTTP ${r.status}): ${text || 'empty response'}`); }
    if (!r.ok) throw new Error(value.error);
    return value;
  }
  let state, plan;
  function toolbar(show) {
    $('#toolbar').hidden = !show;
    $('#handle').setAttribute('aria-expanded', String(show));
    $('#handle').setAttribute('aria-label', (show ? 'Hide' : 'Show') + ' sandbox toolbar');
  }
  $('#handle').onclick = () => {
    $('#handle').focus();
    if (!$('#drawer').hidden) { $('#drawer').hidden = true; $('#settings').setAttribute('aria-expanded', 'false'); }
    toolbar($('#toolbar').hidden);
  };
  window.addEventListener('blur', () => {
    setTimeout(() => { if (document.activeElement === $('#editor')) toolbar(false); }, 0);
  });
  $('#fullscreen').hidden = !document.fullscreenEnabled;
  $('#fullscreen').onclick = async () => {
    const exiting = Boolean(document.fullscreenElement);
    try { if (exiting) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch(e) { toolbar(false); $('#drawer').hidden = false; $('#status').dataset.error = 'true'; $('#status').textContent = `Could not ${exiting ? 'exit' : 'enter'} full screen: ${e.message}`; }
  };
  document.addEventListener('fullscreenchange', () => { $('#fullscreen').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen'; });
  function dirty() { plan = null; $('#review').hidden = true; $('#apply').textContent = 'Review change'; $('#apply').disabled = $('#network').value === state.network; }
  function close() { $('#status').textContent = ''; delete $('#status').dataset.error; if (state) { $('#network').value = state.network; dirty(); } $('#drawer').hidden = true; $('#settings').setAttribute('aria-expanded', 'false'); $('#handle').focus(); }
  try {
    state = await request('/state');
    $('#network-label').textContent = state.label;
    if (location.search === '?settings-only') {
      document.querySelector('main').hidden = true; $('#drawer').hidden = false; toolbar(true);
    } else { $('#editor').src = state.editor; }
    $('#mode').textContent = state.label + ': ' + (state.network === 'allowlist' ? 'Restricted' : 'Unrestricted');
    $('#network').value = state.network;
    $('#settings').onclick = () => { $('#drawer').hidden = false; $('#settings').setAttribute('aria-expanded','true'); toolbar(false); $('#network').focus(); };
    $('#close').onclick = close;
    $('#cancel').onclick = () => { $('#network').value = state.network; dirty(); close(); };
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (!$('#drawer').hidden) close();
      else if (!$('#toolbar').hidden) { toolbar(false); $('#handle').focus(); }
    });
    $('#network').onchange = dirty;
    $('#apply').onclick = async () => {
      $('#apply').disabled = true; $('#status').textContent = ''; delete $('#status').dataset.error;
      try {
        if (!plan) {
          plan = await request('/preview', {network: $('#network').value});
          $('#change').textContent = `${state.label}: ${state.network === 'allowlist' ? 'Restricted' : 'Unrestricted'} → ${$('#network').selectedOptions[0].textContent}`;
          $('#effect').replaceChildren(...plan.notes.map(note => { const item=document.createElement('li'); item.textContent=note; return item; }));
          $('#review').hidden = false;
          $('#apply').textContent = 'Apply'; $('#apply').disabled = false;
        } else {
          $('#status').textContent = 'Applying…';
          state = await request('/apply', {plan: plan.id});
          $('#mode').textContent = state.label + ': ' + (state.network === 'allowlist' ? 'Restricted' : 'Unrestricted');
          $('#status').textContent = 'Demo setting updated. No live sandbox was changed.';
          dirty();
        }
      } catch(e) { $('#status').dataset.error = 'true'; $('#status').textContent = e.message; dirty(); }
    };
  } catch(e) { $('#drawer').hidden=false; $('#status').textContent=e.message; }
})();
