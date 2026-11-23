// Keep VS Code's non-recursive file watchers usable when Linux watch quotas run out.
// Native watching remains the default. Poll only the exact paths that failed.
const fs = require('node:fs');
const path = require('node:path');
const {fileURLToPath} = require('node:url');
const {EventEmitter} = require('node:events');
const {syncBuiltinESMExports} = require('node:module');
const nativeWatch = fs.watch;
let reported = false;

function pollWatch(filename, options, listener, interval = 2000) {
  const target = filename instanceof URL ? fileURLToPath(filename) : filename.toString();
  const opts = typeof options === 'string' ? {encoding: options} : options || {};
  const watcher = new EventEmitter();
  let closed = false, busy = false;
  const fingerprint = stat => `${stat.dev}:${stat.ino}:${stat.mode}:${stat.size}:${stat.mtimeMs}:${stat.ctimeMs}`;
  function snapshot() {
    const stat = fs.statSync(target);
    const result = new Map();
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(target)) {
        try { result.set(entry, fingerprint(fs.lstatSync(path.join(target, entry)))); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
    } else result.set(path.basename(target), fingerprint(stat));
    return result;
  }
  // Establish the baseline before returning, just as fs.watch starts immediately.
  let previous = snapshot();
  function changed(kind, name) {
    if (!closed) watcher.emit('change', kind, opts.encoding === 'buffer' ? Buffer.from(name) : name);
  }
  async function nextSnapshot() {
    const stat = await fs.promises.stat(target);
    if (!stat.isDirectory()) return new Map([[path.basename(target), fingerprint(stat)]]);
    const names = await fs.promises.readdir(target);
    const result = new Map();
    // Bound filesystem concurrency and keep a large directory off the JS event loop.
    for (let offset = 0; offset < names.length && !closed; offset += 16) {
      await Promise.all(names.slice(offset, offset + 16).map(async name => {
        try { result.set(name, fingerprint(await fs.promises.lstat(path.join(target, name)))); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      }));
    }
    return result;
  }
  const timer = setInterval(async () => {
    if (closed || busy) return;
    busy = true;
    try {
      let current;
      try { current = await nextSnapshot(); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        changed('rename', path.basename(target));
        watcher.close();
        return;
      }
      for (const [name, value] of current) {
        if (!previous.has(name)) changed('rename', name);
        else if (previous.get(name) !== value) changed('change', name);
      }
      for (const name of previous.keys()) if (!current.has(name)) changed('rename', name);
      previous = current;
    } catch (error) { if (!closed) watcher.emit('error', error); }
    finally { busy = false; }
  }, interval);
  watcher.close = () => {
    if (closed) return;
    closed = true; clearInterval(timer);
    opts.signal?.removeEventListener('abort', watcher.close);
    queueMicrotask(() => watcher.emit('close'));
  };
  watcher.ref = () => { timer.ref(); return watcher; };
  watcher.unref = () => { timer.unref(); return watcher; };
  if (opts.persistent === false) watcher.unref();
  if (listener) watcher.on('change', listener);
  if (opts.signal?.aborted) watcher.close();
  else opts.signal?.addEventListener('abort', watcher.close, {once: true});
  return watcher;
}

function install(watch = nativeWatch) {
  fs.watch = function(filename, options, listener) {
    if (typeof options === 'function') { listener = options; options = {}; }
    try { return watch.call(fs, filename, options, listener); }
    catch (error) {
      if (!['EMFILE', 'ENFILE', 'ENOSPC'].includes(error.code) || options?.recursive) throw error;
      const watcher = pollWatch(filename, options, listener);
      if (!reported) {
        reported = true;
        console.warn('[Factory] Native file watching is unavailable; affected editor paths will update every 2 seconds.');
      }
      return watcher;
    }
  };
  syncBuiltinESMExports();
}
module.exports = {pollWatch, install};
install();
