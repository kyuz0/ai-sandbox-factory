(async () => {
  const $ = selector => document.querySelector(selector);
  const pairing = new URLSearchParams(location.hash.slice(1)).get('pair');
  history.replaceState(null, '', location.pathname);
  // Chromium copies sessionStorage into editor-opened windows. The random
  // top-level context name is not copied and cannot be read by the editor.
  const binding = sessionStorage.getItem('factory-window');
  if (!binding || window.name !== binding) sessionStorage.removeItem('factory-control');
  let token = sessionStorage.getItem('factory-control') || '';
  async function request(path, body = {}) {
    const response = await fetch(path, {method:'POST', credentials:'omit',
      headers:{'Authorization':`Bearer ${token}`, 'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const text = await response.text();
    let value;
    try { value = JSON.parse(text); }
    catch { throw new Error(`Factory request failed (HTTP ${response.status}): ${text || 'empty response'}`); }
    if (!response.ok) throw new Error(value.error);
    return value;
  }
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  let state, section, plan, pending = false, applying = false, generation = 0, displayedJob, categoryKey;
  function status(text, error = false) {
    $('#status').textContent = text;
    $('#status').dataset.error = String(error);
  }
  function toolbar(show) {
    $('#toolbar').hidden = !show;
    $('#handle').setAttribute('aria-expanded', String(show));
    $('#handle').setAttribute('aria-label', `${show ? 'Hide' : 'Show'} sandbox toolbar`);
  }
  function values() {
    return Object.fromEntries((section?.fields || []).map(field => {
      const input = document.getElementById(field.key);
      return [field.key, field.type === 'checkbox' ? input.checked : field.type === 'multi'
        ? [...input.selectedOptions].map(option=>option.value) : input.value];
    }));
  }
  function changed() {
    if (!section) return false;
    const current = values();
    return section.fields.some(field => modified(field,current[field.key]));
  }
  function modified(field,value) {
    const normalized = item => field.type === 'multi' ? [...item].sort() : item;
    return JSON.stringify(normalized(value)) !== JSON.stringify(normalized(field.value));
  }
  function controls() {
    const current = section ? values() : {};
    for (const field of section?.fields || []) {
      document.getElementById(field.key).disabled = pending || applying || Boolean(field.depends && !current[field.depends]);
      if (field.depends) document.getElementById(field.key+'-dependency').hidden = Boolean(current[field.depends]);
    }
    $('#apply').hidden = !section;
    $('#apply').disabled = pending || applying || !changed();
    $('#apply').textContent = applying ? 'Applying…' : plan ? (section?.id === 'packages' ? 'Install packages' : 'Apply') : 'Review change';
  }
  function dirty() {
    generation++;
    pending = false;
    plan = null;
    $('#review').hidden = true;
    controls();
  }
  function categories() {
    section = null;
    $('#section').hidden = true;
    $('#categories').hidden = false;
    const key = JSON.stringify((state?.controls || []).map(({id,label})=>[id,label]));
    // Polling must not replace a focused category button with an identical one.
    if (key !== categoryKey) $('#categories').replaceChildren(...(state?.controls || []).map(group => {
      const button = element('button',group.label);
      button.dataset.section = group.id;
      button.onclick = () => choose(group.id);
      return button;
    }));
    categoryKey = key;
    dirty();
  }
  function choose(id) {
    section = state.controls.find(group=>group.id === id);
    if (!section) return categories();
    $('#categories').hidden = true;
    $('#section').hidden = false;
    $('#section-title').textContent = section.label;
    $('#fields').replaceChildren(...section.fields.map(field => {
      const group = element('div',undefined,'setting');
      const label = element('label',field.label);
      label.htmlFor = field.key;
      const input = element(field.type === 'select' || field.type === 'multi' ? 'select' : field.type === 'textarea' ? 'textarea' : 'input');
      input.id = field.key;
      if (field.type === 'checkbox') {
        input.type = 'checkbox'; input.checked = field.value;
        label.prepend(input); group.append(label);
      } else {
        if (field.type === 'text') input.type = 'text';
        if (field.choices.length) input.replaceChildren(...field.choices.map(([text,value]) => new Option(text,value)));
        if (field.type === 'multi') {
          input.multiple = true;
          for (const option of input.options) option.selected = field.value.includes(option.value);
        } else input.value = field.value;
        group.append(label,input);
      }
      input.addEventListener('input',dirty);
      input.addEventListener('change',dirty);
      if (field.depends) {
        const dependency = section.fields.find(candidate=>candidate.key === field.depends);
        const hint = element('small',`Enable ${dependency.label} first.`,'dependency');
        hint.id = field.key+'-dependency'; input.setAttribute('aria-describedby',hint.id); group.append(hint);
      }
      return group;
    }));
    const help = section.fields.filter(field=>field.help);
    $('#help').hidden = !help.length;
    $('#help').open = false;
    $('#help-content').replaceChildren(...help.map(field => {
      const paragraph = element('p');
      paragraph.append(element('strong',field.label + '. '),document.createTextNode(field.help));
      return paragraph;
    }));
    dirty();
    $('#fields input, #fields select, #fields textarea')?.focus();
  }
  function close() {
    if (!applying) status('');
    categories();
    $('#drawer').hidden = true;
    $('#settings').setAttribute('aria-expanded','false');
    $('#handle').focus();
  }
  $('#section-back').onclick = () => { categories(); $('#categories button')?.focus(); };
  $('#handle').onclick = () => {
    if (!$('#drawer').hidden) { $('#drawer').hidden = true; $('#settings').setAttribute('aria-expanded','false'); }
    toolbar($('#toolbar').hidden);
  };
  $('#settings').onclick = () => {
    $('#drawer').hidden = false; $('#settings').setAttribute('aria-expanded','true'); toolbar(false);
    if (section) $('#fields input, #fields select, #fields textarea')?.focus();
    else $('#categories button')?.focus();
  };
  $('#close').onclick = close;
  $('#cancel').onclick = close;
  window.addEventListener('blur',()=>setTimeout(()=>{ if (document.activeElement === $('#editor')) toolbar(false); },0));
  document.addEventListener('keydown',event=>{
    if (event.key !== 'Escape') return;
    if (!$('#drawer').hidden) close();
    else if (!$('#toolbar').hidden) { toolbar(false); $('#handle').focus(); }
  });
  $('#fullscreen').hidden = !document.fullscreenEnabled;
  $('#fullscreen').onclick = async () => {
    const exiting = Boolean(document.fullscreenElement);
    try { if (exiting) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch (error) { toolbar(false); $('#drawer').hidden = false; status(`Could not ${exiting ? 'exit' : 'enter'} full screen: ${error.message}`,true); }
  };
  document.addEventListener('fullscreenchange',()=>{
    $('#fullscreen').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen';
  });
  function render(next) {
    const initial = !state;
    state = next;
    document.title = `${state.name} · Factory`;
    $('#sandbox-name').textContent = state.name;
    $('#settings-name').textContent = state.name;
    const network = state.controls.find(group=>group.id === 'network').fields[0];
    $('#mode').textContent = `${network.label}: ${network.choices.find(([,value])=>value === state.network)[0].split(' — ')[0]}`;
    if (initial || (!section && categoryKey !== JSON.stringify(state.controls.map(({id,label})=>[id,label])))) categories();
    applying = state.job?.status === 'running';
    const editorChanged = state.editor && $('#editor').getAttribute('src') !== state.editor;
    if (editorChanged) $('#editor').src = state.editor;
    $('#connection').textContent = state.editor || applying ? '' : 'The browser editor is not running. Open this sandbox from Factory to reconnect.';
    if (applying) {
      sessionStorage.setItem('factory-watched-job',state.job.id);
      status(`${state.job.title || 'Settings'}: applying. You can close this panel; the operation will continue.`);
    } else if (state.job && displayedJob !== state.job.id && sessionStorage.getItem('factory-watched-job') === state.job.id) {
      displayedJob = state.job.id;
      sessionStorage.removeItem('factory-watched-job');
      const selected = section?.id;
      // Replacement failure can restore the previous editor. Reconnect after
      // either outcome, without reloading for a live settings/package change.
      if (state.job.effect === 'replace' && !editorChanged && state.editor && sessionStorage.getItem('factory-reloaded-job') !== state.job.id) {
        $('#editor').src = state.editor;
      }
      sessionStorage.setItem('factory-reloaded-job',state.job.id);
      if (state.job.status === 'succeeded') {
        status(state.job.completion || 'Settings updated.');
        if (selected) choose(selected); else categories();
      } else {
        status(state.job.error,true);
        $('#drawer').hidden = false;
        $('#settings').setAttribute('aria-expanded','true');
        toolbar(false);
        dirty();
      }
      $('#job-output').hidden = !state.job.output;
      $('#job-output pre').textContent = state.job.output || '';
    }
    controls();
  }
  function showReview(result) {
    $('#change').replaceChildren(...(result.summary || []).map(change=>{
      const display = value => change.type === 'checkbox' ? (value ? 'On' : 'Off')
        : (change.choices.find(([,key])=>key === value)?.[0] || value || 'None');
      if (change.type === 'textarea' || change.type === 'multi') {
        const details = element('details');
        const text = value => Array.isArray(value) ? value.map(item=>display(item)).join('\n') || 'None' : value || 'None';
        details.append(element('summary',change.label),element('strong','Current'),element('pre',text(change.before)),
                       element('strong','Selected'),element('pre',text(change.after)));
        return details;
      }
      return element('p',`${change.label}: ${display(change.before)} → ${display(change.after)}`);
    }));
    if (result.packages) $('#change').append(element('p',`Install: ${result.packages.join(', ')}`));
    $('#effect').replaceChildren(...result.notes.map(note=>element('li',note)));
    $('#review').hidden = false;
  }
  $('#apply').onclick = async () => {
    const owner = ++generation;
    pending = true; controls(); status(''); $('#job-output').hidden = true;
    try {
      if (!plan) {
        const current = values();
        const edits = Object.fromEntries(section.fields.filter(field=>modified(field,current[field.key])).map(field=>[field.key,current[field.key]]));
        const result = await request('/preview',{section:section.id, values:edits});
        if (owner !== generation) return;
        plan = result; showReview(result);
      } else {
        const result = await request('/apply',{plan:plan.id});
        // A submitted job belongs to the sandbox even if its panel was closed.
        if (result.job) render({...state,job:result.job}); else status(result.notes.join(' '));
        plan = null; $('#review').hidden = true;
      }
    } catch (error) {
      if (owner !== generation) return;
      status(error.message,true); plan = null; $('#review').hidden = true;
    } finally { if (owner === generation) pending = false; controls(); }
  };
  controls();
  try {
    if (pairing) {
      token = (await request('/pair',{code:pairing})).token;
      window.name = 'factory-' + crypto.randomUUID();
      sessionStorage.setItem('factory-window',window.name);
      sessionStorage.setItem('factory-control',token);
    }
    render(await request('/state'));
    async function poll() {
      try { render(await request('/state')); }
      catch (error) { $('#connection').textContent = error.message; }
      setTimeout(poll,1500);
    }
    setTimeout(poll,1500);
  } catch (error) { $('#drawer').hidden = false; status(error.message,true); }
})();
