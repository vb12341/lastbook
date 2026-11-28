// Runs web/setup.js in a vm with a tiny fake page, to check that the screen asks again while the library list is empty.
// Usage: node app/tests/setup_js_test.mjs   (exit code 1 on a failed check)
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const src = readFileSync(process.env.SETUP_JS || path.join(path.dirname(fileURLToPath(import.meta.url)), '../../web/setup.js'), 'utf8');   // SETUP_JS: another copy to try

const flush = () => new Promise(r => setTimeout(r, 5));
function page(answers) {
  const els = {}, timers = [], calls = [];
  let acts = {};
  const el = sel => els[sel] || (els[sel] = {
    sel, innerHTML: '', textContent: '', hidden: false, disabled: false, dataset: {}, style: {}, firstChild: { style: {} },
    classList: { contains: () => false, add() { }, remove() { } }, focus() { }, contains: () => false, setAttribute() { }, removeAttribute() { },
    querySelector: q => q === 'h1' ? { focus() { } } : null,
    querySelectorAll(q) {   // buttons with a data-act in what the screen drew, so a click can be sent to them
      if (q !== '[data-act]') return [];
      acts = {};
      return [...this.innerHTML.matchAll(/data-act="([^"]+)"/g)].map(m => (acts[m[1]] = { dataset: { act: m[1] }, disabled: false }));
    }, get offsetWidth() { return 0 } });
  const ctx = vm.createContext({
    document: { querySelector: el, documentElement: { classList: { contains: () => false } } },
    location: { hash: '' }, navigator: { platform: 'MacIntel' }, CSS: { escape: s => s }, Object, JSON, Promise, Set, Math, Number, String, Array, Error, Boolean, RegExp, isFinite,
    scrollTo() { }, addEventListener() { }, clearTimeout() { },
    setTimeout: (f, ms) => { timers.push(f); return timers.length },
    fetch: async (url, opts) => {
      const name = url.replace('/api/setup/', ''), body = opts && opts.body ? JSON.parse(opts.body) : undefined;
      calls.push([name, body]);
      const a = answers[name];
      const d = await (typeof a === 'function' ? a(body, calls) : a);   // an answer can be a promise, to hold it back
      return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(d === undefined ? null : d)) };
    },
  });
  vm.runInContext(src, ctx, { filename: 'setup.js' });
  return { el, timers, calls, ctx, acts: () => acts };
}
const pack = { id: 'small', title: 'Small', available: true, summary: 's', installed_bytes: 1e9, download_bytes: 5e8, files: [] };
const state = (extra = {}) => ({ mode: 'first-run', hardware: { disks: [], ai: { fits: false } }, manifest: { packs: [pack], models: [], maps: [] }, current: null, found: [], recommend: { folder: '/x' }, job: null, ...extra });

let bad = 0;
const check = (name, ok) => { if (!ok) { bad++; console.log('FAIL', name) } else console.log('ok  ', name) };

{   // an empty manifest is asked for again
  let n = 0;
  const p = page({ state: () => (++n === 1 ? state({ manifest: { packs: [], models: [], maps: [] } }) : state()) });
  await flush();
  check('empty list schedules another ask', p.timers.length === 1);
  p.timers[0](); await flush();
  check('second state fills the list', p.el('#main').innerHTML.includes('Small'));
  check('and stops asking', p.timers.length === 1);
}

{   // an install starts while the second ask is on its way: its answer must not stop the progress polling
  let n = 0;
  const running = { state: 'downloading', items: [{ kind: 'pack', id: 'small', state: 'downloading' }], overall: {} };
  const p = page({ state: () => (++n === 1 ? state({ manifest: { packs: [], models: [], maps: [] } }) : state({ job: running })), job: running });
  await flush();
  p.timers[0](); await flush();
  const before = p.calls.filter(c => c[0] === 'job').length;
  p.timers[p.timers.length - 1](); await flush();
  check('a running job is still polled after the second ask', p.calls.filter(c => c[0] === 'job').length > before);
}

{   // a library file is chosen while the second ask is on its way: that older answer must not replace the install screen
  let n = 0, release;
  const running = { state: 'downloading', items: [{ kind: 'pack', id: 'small', state: 'downloading' }], overall: {} };
  const empty = () => state({ manifest: { packs: [], models: [], maps: [] } });
  const p = page({ state: () => (++n === 1 ? empty() : new Promise(r => { release = () => r(empty()) })), pick: { path: '/x/lib.zip' }, use: running, job: running });
  await flush();
  p.timers[0](); await flush();                   // the second ask is held back
  p.el('#haveFile').onclick(); await flush();     // the install starts and draws its progress
  const shown = p.el('#main').innerHTML;
  release(); await flush();                       // the older answer arrives
  check('an older answer leaves the install screen alone', p.el('#main').innerHTML === shown);
  const before = p.calls.filter(c => c[0] === 'job').length;
  p.timers[p.timers.length - 1](); await flush();
  check('and the install is still polled', p.calls.filter(c => c[0] === 'job').length > before);
}

{   // Cancel while the work cannot stop yet: "Finishing cancellation…" stays until the engine says it has stopped (#463 review)
  const running = { state: 'unpacking', items: [{ kind: 'pack', id: 'small', state: 'unpacking' }], overall: {} };
  const gone = { state: 'failed', error: { code: 'cancelled', message: 'Cancelled.' }, items: [{ kind: 'pack', id: 'small', state: 'failed' }], overall: {} };
  let release, stopped = false;
  const p = page({ state: () => state({ job: stopped ? gone : running }), job: () => (stopped ? gone : { ...gone, stopping: true }),
    cancel: () => new Promise(r => { release = () => r({ ...gone, stopping: true }) }) });
  p.el('#main').querySelector = q => q === 'h1' || q.startsWith('[data-act') ? { focus() { } } : null;
  await flush();
  p.acts().cancel.onclick(); await flush();
  p.acts()['cancel-yes'].onclick(); await flush();
  const finishing = () => p.el('#main').innerHTML.includes('Finishing cancellation');
  check('Stop shows "Finishing cancellation" while the engine waits', finishing());
  release(); await flush();
  check('and keeps it while the work is still blocked', finishing());
  p.timers[p.timers.length - 1](); await flush();
  check('a poll that still says stopping keeps it', finishing());
  stopped = true;
  p.timers[p.timers.length - 1](); await flush();
  check('it clears to Setup once the work has stopped', !finishing() && p.el('#main').innerHTML.includes('Small'));
}

{   // a Cancel the engine refuses leaves the job screen up: it does not say "cancelled" while the work runs on
  const running = { state: 'downloading', items: [{ kind: 'pack', id: 'small', state: 'downloading' }], overall: {} };
  const p = page({ state: () => state({ job: running }), job: running, cancel: { error: 'Lastbook is still stopping the previous download.', code: 'busy' } });
  p.el('#main').querySelector = q => q === 'h1' || q.startsWith('[data-act') ? { focus() { } } : null;
  await flush();
  p.acts().cancel.onclick(); await flush();
  p.acts()['cancel-yes'].onclick(); await flush();
  check('a refused cancel stays on the job', p.el('#main').innerHTML.includes('data-act="cancel"') && p.el('#live').textContent !== 'The download was cancelled.');
}

{   // Codex on #463: a busy Retry shows the job as it is now and keeps polling it, instead of freezing on the old screen
  const failed = { state: 'failed', error: { code: 'network', message: 'x' }, items: [{ kind: 'pack', id: 'small', state: 'failed' }], overall: {} };
  const running = { state: 'downloading', items: [{ kind: 'pack', id: 'small', state: 'downloading' }], overall: {} };
  const p = page({ state: () => state({ job: failed }), job: running, retry: { error: 'Lastbook is still stopping the previous download.', code: 'busy' } });
  p.el('#main').querySelector = q => q === 'h1' || q.startsWith('[data-act') ? { focus() { } } : null;
  await flush();
  p.acts().retry.onclick(); await flush();
  check('a busy retry shows the job as it is now', p.el('#main').innerHTML.includes('data-act="cancel"'));
  const before = p.calls.filter(c => c[0] === 'job').length;
  (p.timers.at(-1) || (() => { }))(); await flush();
  check('and keeps polling it', p.calls.filter(c => c[0] === 'job').length > before);
}

{   // asking again in the background must not move focus to the heading or scroll to the top
  let n = 0;
  const p = page({ state: () => (++n <= 9 ? state({ manifest: { packs: [], models: [], maps: [] } }) : state()) });
  let focuses = 0, scrolls = 0;
  p.el('#main').querySelector = q => q === 'h1' ? { focus() { focuses++ } } : null;
  p.ctx.scrollTo = () => { scrolls++ };
  await flush();
  for (let i = 0; i < 8 && p.timers.length; i++) { p.timers.shift()(); await flush() }
  console.log(`     (focus ${focuses}, scroll ${scrolls})`);
  check('background asks leave focus alone', focuses === 1);
  check('and leave the scroll position alone', scrolls === 1);
}

{   // "Use this file": it goes to the folder chosen on screen, with nothing to download; a noted map does not tick the AI
  const p = page({ state: state(), pick: { path: '/d/Lastbook-Map-CA.zip' }, use: { noted: true, kind: 'map', job: null } });
  await flush();
  check('the chooser says a zip installs on its own', p.el('#main').innerHTML.includes('A library zip is installed as it is, on its own'));
  p.el('#haveFile').onclick(); await flush();
  const use = p.calls.find(c => c[0] === 'use');
  check('use sends the path and the chosen folder', use && use[1].path === '/d/Lastbook-Map-CA.zip' && use[1].folder === '/x/LastbookData');
  check('and nothing to download with it', use && !('ai' in use[1]) && !('map' in use[1]) && !('address_id' in use[1]));
  check('a noted map is not the AI file', p.el('#haveNote').textContent.includes('map file') && !p.el('#haveNote').textContent.includes('AI'));
  const note = p.el('#haveNote').textContent;   // wonderdawg on #465: say which action uses it, since a library zip does not
  check('the note names the button that uses the map', note.includes('“Download and install”, once your state is chosen'));
  check('and says a library zip does not', note.includes('A library zip') && !note.includes('will be used.'));
}

process.exit(bad ? 1 : 0);
