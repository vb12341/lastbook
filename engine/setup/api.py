"""/api/setup/*: the engine side of first-run setup and of Library → Library and local AI. Contract: docs/SETUP-API.md.

Two ways in, one module:
  - setup mode (no library yet): app/main.py makes a Bridge around SetupServer, whose Handler serves web/setup.html at /, the web
    folder at /ui/, and /api/setup/* here. Nothing else.
  - normal mode: server.py hands /api/setup/ to handle(). The app's main.py configures it first (configure(...)); a bare
    server.py gets a manage-mode Setup on first use, with no relaunch (finish then answers restart: true).

Only requests from this computer get an answer (the bridge's, or loopback): a phone the guide is shared with gets 403.
"""
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .. import model_server
from . import download, hardware, install
from .manifest import Manifest

DATA_NAME = install.DATA_NAME
WORK_SUFFIX = '.setup'
SAVE_EVERY = 2.0
RECOMMEND_MARGIN = 5 * 10 ** 9     # beyond what the install needs, so the disk is not left full
RECOMMEND_MARGIN_MAX = 20 * 10 ** 9
MAX_BODY = 64 * 1024
ACTIVE = ('waiting', 'downloading', 'verifying', 'unpacking', 'activating', 'copying')
FAT_MAX = 4 * 2 ** 30 - 1        # the largest file FAT32 can hold
CLOUD_RECHECK_S = 600             # how long a count of the library's iCloud-only files is kept before the next one (#362)
COPY_MARK = '.lastbook-copy-in-progress'   # in a copy's LastbookData until it is complete: copying there again carries on
START_HERE = """Lastbook: an offline survival library

This drive holds a copy of a Lastbook library: the {title} library, {size} in {files} files, copied {date}.
It works with no internet: manuals, videos, encyclopedias and maps.

To use it on another computer:
1. Install the Lastbook app on that computer. This drive does not hold the app: get it from
   lastbook.org, or from someone who has the installer.
2. Open Lastbook. In setup, choose to use a library that is already on this computer,
   and choose the folder called {data} on this drive. You can copy that folder onto the
   computer first, or use it from the drive.
3. To make sure every file arrived whole, use Library and local AI, then Check and repair.
   It reads every file, and downloads something only if a file is damaged.

This drive holds only the library's own files, as they were published. Nothing personal
from the computer it was copied from is on it: no settings, saved pages, chats, notes,
address search or local AI.
"""


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _gb(n):
    return f'{n / 1e9:.0f} GB' if n >= 10e9 else f'{n / 1e9:.1f} GB'


def _sha256(path, control, progress):
    h, done = hashlib.sha256(), 0
    total = os.path.getsize(path)
    with open(path, 'rb') as f:
        while True:
            control.check()
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            progress(done, total)
    return h.hexdigest()


def _drop_cache(fd):
    """Ask the system to forget its cached copy of a file just written, so reading it back comes from the drive. Best effort:
    Linux drops the pages; elsewhere the read-back may come partly from the cache."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def _read_back(path, control, progress):
    """SHA-256 of a file as the drive gives it back. On a Mac the read skips the cache (F_NOCACHE)."""
    h, done = hashlib.sha256(), 0
    with open(path, 'rb') as f:
        if sys.platform == 'darwin':
            try:
                import fcntl
                fcntl.fcntl(f.fileno(), 48, 1)      # F_NOCACHE
            except (ImportError, OSError):
                pass
        _drop_cache(f.fileno())
        while True:
            control.check()
            b = f.read(8 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
            progress(done)
    return h.hexdigest()


def _dest_ok(dst, rel):
    """A copy writes only inside its own LastbookData on the drive. Refuses when the copy folder, any folder on the way to rel, the
    file, or its .partial is a link: a link left on a drive could otherwise send the copy into someone else's folder."""
    if dst.is_symlink():
        raise install.Refused('copy', f'The {DATA_NAME} folder on the drive is a link, so nothing was written there. Choose another drive or folder.')
    p = dst
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            raise install.Refused('copy', f'On the drive, {rel} goes through a link that could lead outside the copy, so nothing more was '
                                          'written. Choose another drive or folder.')
    if p.with_name(p.name + '.partial').is_symlink():
        raise install.Refused('copy', f'On the drive, {rel}.partial is a link, so nothing more was written. Choose another drive or folder.')


def _inside(dst, path):
    root, real = dst.resolve(), Path(path).resolve()
    return real == root or root in real.parents


def _open_new(path):
    """Open a file for writing without following a link to it (O_NOFOLLOW where the system has it)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    return os.fdopen(os.open(path, flags, 0o644), 'wb')


def _ours(path):
    """Is this START-HERE.txt one a Lastbook copy wrote?"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.readline().strip() == START_HERE.splitlines()[0]
    except OSError:
        return False


def _same_disk(a, b):
    try:
        pa, pb = Path(a), Path(b)
        while not pa.exists():
            pa = pa.parent
        while not pb.exists():
            pb = pb.parent
        return os.stat(pa).st_dev == os.stat(pb).st_dev
    except OSError:
        return False


STOPPING = {'error': 'Lastbook is still stopping the previous download. Try again in a moment.', 'code': 'busy'}     # a cancelled worker outlived the join
FINISHING = 'Cancelled. Lastbook is finishing the cancellation: the files of this download are cleared when the step it is on ends.'


class _Item:
    """Progress of one item (the pack, or the model), with rate and time left."""

    def __init__(self, d, stale=lambda: False):
        self.d, self.stale = d, stale      # stale: the worker reporting is no longer the job's (cancelled), so it changes nothing
        self.t, self.at, self.rate = time.time(), d.get('bytes_done', 0), 0.0

    def progress(self, done, total, state=None):
        d = self.d
        if self.stale():
            return
        d.pop('retry_at', None)
        if state and d['state'] != state:
            d['state'] = state
            self.t, self.at, self.rate = time.time(), done, 0.0
        d['bytes_done'], d['bytes_total'] = done, total
        now = time.time()
        if now - self.t >= 1.0:
            inst = max(done - self.at, 0) / (now - self.t)
            self.rate = inst if not self.rate else 0.6 * self.rate + 0.4 * inst
            self.t, self.at = now, done
            d['rate_bps'] = int(self.rate)
            d['eta_s'] = round((total - done) / self.rate) if self.rate > 0 else None

    def waiting(self, seconds):
        """The server is busy: nothing arrives for `seconds`, so there is no rate, and the time of the next try is kept."""
        d = self.d
        if self.stale():
            return
        d['retry_at'], d['rate_bps'], d['eta_s'] = time.time() + seconds, 0, None
        self.t, self.at, self.rate = time.time(), d.get('bytes_done', 0), 0.0


class Setup:
    def __init__(self, mode, settings_dir, default_parent, current=None, manifest=None, allowed=None, context=None,
                 on_finish=None, pick_folder=None, look_for_libraries=None, home=None, llm_local=None, default_why=None,
                 on_model_server=None):
        """mode: 'first-run' or 'manage'. current: the open library (manage). on_finish(folder): the app opens that library
        (it relaunches). pick_folder(): a folder the person chose, or None. look_for_libraries(): LastbookData folders found."""
        self.mode, self.current = mode, Path(current) if current else None
        self.settings_dir, self.default_parent = Path(settings_dir), Path(default_parent)
        self.manifest = manifest or Manifest(allowed=allowed, context=context)
        self.allowed, self.context = allowed, context
        self.on_finish, self.pick_folder, self.look_for_libraries = on_finish, pick_folder, look_for_libraries
        self.default_why = default_why     # why the default folder is not the usual one (a long Windows user folder), for the screen
        self.on_model_server = on_model_server   # (base, model) or (None, None): the open library's assistant switches at once (manage)
        self._llm = llm_local
        self.lock = threading.RLock()
        self.choice_lock = threading.Lock()    # one model-server choice at a time: saved and switched in the same order (two windows)
        self.job, self.control, self.thread = None, None, None
        self._worker = threading.local()      # .job, .run: the job and the run token the current worker thread started with
        self._run_id = 0      # bumped by every worker start and by cancel: a worker holding an older one is stale
        self._cancelling = None     # the job cancel() is waiting on: it is reported as stopping from the click, in every window
        self.sources = {}          # file name -> a file already on this computer the person chose with `use`
        self.ai_measure, self.measuring = None, False
        self.deleting = set()
        self.rejected = {}         # path -> (size, mtime) of a file already here that failed its SHA-256: not offered or used again
        self.cloud, self.cloud_thread = None, None   # (folder, time, hardware.cloud_only) of the open library, counted in the background
        self.job = self._load_job()

    # ------------------------------------------------------------------------------------------------- saved state --
    def _settings(self):
        try:
            return json.loads((self.settings_dir / 'setup.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_settings(self, d):
        try:
            self.settings_dir.mkdir(parents=True, exist_ok=True)
            install._write_json(self.settings_dir / 'setup.json', d)
        except OSError as e:
            print('setup: could not save setup.json:', e, flush=True)

    def _load_job(self):
        """The saved job, from the app's own settings folder only, with its plan rebuilt from the manifest and the work folder
        recomputed. Anything that does not check out is no job: it never stops the app from starting."""
        try:
            job = self._checked_job(json.loads(_job_file(self.settings_dir).read_text(encoding='utf-8')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            if not isinstance(e, FileNotFoundError):
                print('setup: the saved job was not used:', type(e).__name__, flush=True)
            return None
        if not job:
            return None
        if job.get('state') in ACTIVE:      # this program stopped while it ran: carry on only when the person says so
            job['state'] = 'paused'
            for it in job['items']:
                if it['state'] in ACTIVE:
                    was = it['state']
                    it['state'] = 'paused'
                    if was != 'waiting':
                        it['paused_from'] = was       # what it was doing when this program stopped; dropped on resume
                    it['bytes_done'], it['bytes_total'] = self._done_after_restart(job['plan'], it, was)
                it['rate_bps'], it['eta_s'] = 0, None
                it.pop('retry_at', None)
        return job

    def _checked_job(self, job):
        """A job as this program would have written it, or None. Nothing from disk is used as a path or a file name unless it is
        what the plan's ids and the parent folder give: pack, map and model come from the manifest; work and target are worked
        out again; no `verified` list is kept (files are hashed again); a source must be a file of exactly the expected name."""
        plan, items = job['plan'], job['items']
        if not isinstance(plan, dict) or not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return None
        if job.get('state') not in ACTIVE + ('paused', 'done', 'failed'):
            return None
        if plan.get('copy'):     # a copy is not carried over a restart: copying to the same place again carries on where it was
            return None
        if plan.get('use_library'):
            lib = Path(plan['use_library'])
            if not install.is_library(lib):
                return None
            job['plan'] = {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}
            return job
        parent, target = Path(plan['parent']), Path(plan['target'])
        if target.parent != parent or (target.name != DATA_NAME and target != self.current):
            return None          # a library setup installs is called LastbookData; only the open one may have another name
        work = _work_for(target)
        ids = lambda key: (plan.get(key) or {}).get('id')
        pack = self.manifest.pack(ids('pack')) if plan.get('pack') else None
        pack = _pack_download(pack, work, {}, keep_original=not (plan.get('pack') or {}).get('split_archive'))
        mp = self.manifest.map(ids('map')) if plan.get('map') else None
        model = self.manifest.model(ids('model')) if plan.get('model') else None
        addresses = self.manifest.address_index(ids('addresses')) if plan.get('addresses') else None
        if plan.get('addresses') and not addresses:
            return None
        if (plan.get('pack') and not pack) or (plan.get('map') and not mp) or (plan.get('model') and not model):
            return None
        wanted = {('pack', pack['id']) if pack else None, ('map', mp['id']) if mp else None, ('model', model['id']) if model else None, ('addresses', addresses['id']) if addresses else None} - {None}
        if {(i.get('kind'), i.get('id')) for i in items} != wanted:
            return None
        names = ([f['name'] for f in pack['files']] if pack else []) + ([f['name'] for f in mp['files']] if mp else []) + ([f['name'] for f in addresses['files']] if addresses else []) + ([model['file']] if model else [])
        sources = {n: str(v) for n, v in (plan.get('sources') or {}).items()
                   if n in names and isinstance(v, str) and Path(v).name == n and Path(v).is_file()}
        check_only = bool(plan.get('check_only'))
        if check_only:            # Check now stays read-only after a restart: nothing to fetch, switch or take from elsewhere
            if mp or addresses or not plan.get('repair') or target != self.current:
                return None
            sources = {}
        switch = bool(plan.get('switch')) and pack is not None and not check_only
        clean = {'pack': pack, 'model': model, 'map': mp, 'addresses': addresses, 'parent': str(parent), 'target': str(target), 'work': str(work),
                 'sources': sources, 'explicit': bool(plan.get('explicit')), 'retried': {}, 'switch': switch, 'repair': bool(plan.get('repair')),
                 'check_only': check_only}
        if check_only and job['state'] != 'done':
            # Not finished: the check starts again from the installed files. What it saw before the restart is not trusted (a
            # file may have changed since), so nothing it found, and no item it had finished, is carried over.
            job['check'] = True
            for it in items:
                it.update(state='waiting', bytes_done=0, rate_bps=0, eta_s=None, error=None)
                it.pop('paused_from', None)
            job['plan'] = clean
            return job
        if plan.get('repair'):    # what a finished step found, for the result: the model's check, and the state map's
            for k in ('model_ok', 'map_damaged'):
                if isinstance(plan.get(k), bool):
                    clean[k] = plan[k]
        if isinstance(plan.get('damaged'), list):
            clean['damaged'] = [str(x) for x in plan['damaged']]
        if plan.get('map_new'):
            clean['map_new'] = str(work / 'unpack-map' / DATA_NAME)
        if plan.get('model_file') and model:
            clean['model_file'] = str((target if switch else Path(self.current or target)) / 'models' / model['file'])
            if plan.get('model_sha256'):
                clean['model_sha256'] = model['sha256']
        job['plan'] = clean
        return job

    def _done_after_restart(self, plan, it, was):
        """(bytes done, bytes total) for an item found mid-run at startup, from what is on disk for the step it was in."""
        if was == 'unpacking' and it['kind'] == 'map' and plan.get('map'):
            total = plan['map']['installed_bytes']
            return min(_tree_size(Path(plan['work']) / 'unpack-map'), total), total
        if was == 'unpacking' and it['kind'] == 'addresses' and plan.get('addresses'):
            total = plan['addresses']['installed_bytes']
            return min(_tree_size(Path(plan['work']) / 'unpack-addresses'), total), total
        if was == 'unpacking' and plan.get('pack'):
            total = plan['pack']['installed_bytes']
            return min(_tree_size(Path(plan['work']) / 'unpack'), total), total
        if plan.get('check_only'):
            return 0, it['bytes_total']                     # a check starts again; downloads of other jobs are not its progress
        if it.get('source') == 'found':
            return it['bytes_total'], it['bytes_total']     # nothing to download: the file is already on this computer
        return self._on_disk(plan, it), it['bytes_total']

    @staticmethod
    def _on_disk(plan, it):
        """Bytes of this item already downloaded, from the files themselves."""
        folder = Path(plan['work']) / 'downloads'
        split = False
        if it['kind'] == 'pack' and plan.get('pack'):
            plan_files = plan['pack']['files']
            split = bool(plan['pack'].get('split_archive'))
            if split:
                folder = _pack_parts_folder(Path(plan['work']))
        elif it['kind'] == 'map' and plan.get('map'):
            plan_files = plan['map']['files']
        elif it['kind'] == 'addresses' and plan.get('addresses'):
            plan_files = plan['addresses']['files']
        else:
            plan_files = [{'name': plan['model']['file']}] if plan.get('model') else []
        n = 0
        for f in plan_files:
            sizes = [0]
            for p in (folder / f['name'], folder / (f['name'] + '.part')):
                try:
                    sizes.append(p.stat().st_size)
                except OSError:
                    pass
            n += min(f['size'], max(sizes)) if split else sum(sizes)
        return n

    def _save_job(self):
        """To the app's own settings folder. A save that fails stops the job (raises OSError)."""
        job = self.job
        if not job:
            return
        f = _job_file(self.settings_dir)
        f.parent.mkdir(parents=True, exist_ok=True)
        plan = job.get('plan') or {}
        pack = plan.get('pack') or {}
        if pack.get('split_archive'):
            # An interrupted job gets its file pins from the bundled manifest again.
            job = {**job, 'plan': {**plan, 'pack': {'id': pack['id'], 'split_archive': pack['split_archive']}}}
        install._write_json(f, job)

    # --------------------------------------------------------------------------------------------------- the state --
    def llm(self):
        if self._llm is None:
            try:
                import llm_local
                self._llm = llm_local
            except Exception as e:
                print('setup: no llm_local:', type(e).__name__, e, flush=True)
                self._llm = False
        return self._llm or None

    def current_info(self):
        if not self.current:
            return None
        try:
            metadata = json.loads((self.current / 'pack.json').read_text(encoding='utf-8'))
            pack = metadata.get('name')
            revision = metadata.get('content_revision', '')
            if not isinstance(revision, str):
                revision = ''
        except (OSError, ValueError, AttributeError):
            pack, revision = None, ''
        m = self.manifest.model()
        model = None
        if m:
            p = self.current / 'models' / m['file']
            if p.is_file() and p.stat().st_size == m['size']:
                model = m['id']
        return {'folder': str(self.current), 'pack': pack, 'content_revision': revision, 'map_bytes': install.map_bytes(self.current), 'model': model, 'map': install.map_name(self.current), 'address_indexes': self.address_indexes(), 'old': self._settings().get('old', []),
                'checked': self._settings().get('checks', {}).get(str(self.current)),   # the last full check of this library (Check now, Repair)
                **self._cloud_only()}

    def _cloud_only(self):
        """What of the open library is only in iCloud (#362): {'cloud_only': the count or None, 'cloud_counting': True while a first
        count runs}. A large library takes a moment, so the count runs in the background and is kept for CLOUD_RECHECK_S; the page
        asks again only while cloud_counting is True. Other systems cannot tell: None and False, and nothing is started."""
        if sys.platform != 'darwin':
            return {'cloud_only': None, 'cloud_counting': False}
        folder = str(self.current)
        with self.lock:
            kept = self.cloud if self.cloud and self.cloud[0] == folder else None
            if kept and time.time() - kept[1] < CLOUD_RECHECK_S:
                return {'cloud_only': kept[2], 'cloud_counting': False}
            if not (self.cloud_thread and self.cloud_thread.is_alive()):
                def count():
                    r = hardware.cloud_only(folder)
                    with self.lock:
                        self.cloud = (folder, time.time(), r)
                self.cloud_thread = threading.Thread(target=count, daemon=True)
                self.cloud_thread.start()
            return {'cloud_only': kept[2], 'cloud_counting': False} if kept else {'cloud_only': None, 'cloud_counting': True}

    def address_indexes(self):
        if not self.current:
            return []
        out = []
        for entry in self.manifest.current.get('address_indexes', []):
            path = self.current / 'derived' / 'geo' / 'addresses' / (entry['id'] + '.sqlite')
            try:
                receipt = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                expected = {file['name']: file['sha256'] for file in entry['files']}
                if path.is_file() and path.stat().st_size == entry['installed_bytes'] and receipt.get('files') == expected:
                    out.append(entry['id'])
            except (OSError, ValueError, AttributeError):
                pass
        return out

    def found(self):
        """Files in app-owned storage, the selected library, or explicitly chosen by the person. A file is offered when its size matches; its
        SHA-256 is checked when it is used."""
        out, seen = [], set()
        if self.look_for_libraries:
            try:
                for lib in self.look_for_libraries():
                    lib = Path(lib)
                    if str(lib) not in seen and lib != self.current and install.is_library(lib):
                        seen.add(str(lib))
                        out.append({'kind': 'library', 'path': str(lib), 'name': lib.name})
            except Exception:
                traceback.print_exc()
        files = {}
        for p in self.manifest.current['packs']:
            for f in p['files']:
                files[f['name']] = ('pack', p['id'], f['size'])
        m = self.manifest.model()
        if m:
            files[m['file']] = ('model', m['id'], m['size'])
        for kind, key in (('map', 'maps'), ('addresses', 'address_indexes')):
            for x in self.manifest.current.get(key, []):
                for f in x['files']:
                    files[f['name']] = (kind, x['id'], f['size'])
        places = [self.default_parent]
        if self.current:
            places += [self.current / 'models', self.current]
        candidates = [Path(p) for p in self.sources.values()]
        for place in places:
            try:
                candidates.extend(c for c in place.iterdir() if c.name in files or (c.name.startswith('Lastbook-') and c.name.endswith('.zip')))
            except OSError:
                continue
        for c in candidates:
            key = str(c.resolve())
            if key in seen or not c.is_file():
                continue
            seen.add(key)
            kind, pid, size = files.get(c.name, ('pack', None, None))
            st = c.stat()
            rejected = self.rejected.get(str(c)) == (st.st_size, st.st_mtime_ns)
            out.append({'kind': kind, 'path': str(c), 'name': c.name, 'id': pid, 'size': st.st_size,
                        'size_ok': size == st.st_size and not rejected, 'rejected': rejected})
        return out

    def _model_path_here(self):
        m = self.manifest.model()
        if not m:
            return None
        for f in self.found():
            if f['kind'] == 'model' and f.get('size_ok'):
                return f['path']
        return None

    def ai_fit(self, hw):
        m = self.manifest.model()
        if not m:
            return {'fits': False, 'needs_bytes': None, 'estimate': True, 'why': 'No local AI is offered for this version.'}
        if self.ai_measure:
            return self.ai_measure
        path = self._model_path_here()
        if path and not self.measuring and self.llm():
            self.measuring = True
            threading.Thread(target=self._measure, args=(path,), daemon=True, name='setup-measure').start()
        return hardware.estimate_ai(hw['ram_bytes'], hw['gpus'], hw['unified_memory'], m['memory_bytes'])

    def _measure(self, path):
        try:
            r = hardware.measure_ai(self.llm(), path)
            if r:
                self.ai_measure = r
        except Exception:
            traceback.print_exc()

    def hardware(self):
        extra = [self.default_parent] + ([self.current.parent] if self.current else [])
        hw = hardware.detect(extra)
        hw['ai'] = self.ai_fit(hw)
        return hw

    def recommend(self, hw):
        available = [p for p in self.manifest.current['packs'] if p['available']]
        packs = [p for p in available if p['id'] == 'small']
        parent = self.current.parent if self.current else self.default_parent
        free, total = hardware.disk_of(parent)
        model = self.manifest.model()
        need = {p['id']: install.need_bytes(p, None) for p in available}
        map_need = {x['id']: _map_need(x, ()) for x in self.manifest.current.get('maps', []) if x['available']}
        if not packs:
            return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need, 'why': 'No library is ready to download yet.'}
        margin = _margin(total)
        choice, folder, elsewhere = None, parent, None
        for p in packs:        # the disk that holds the home folder (or the open library) whenever anything fits there
            if free >= need[p['id']] + margin:
                choice = p
                break
        if not choice:         # only then another disk for the default Small library
            for p in packs:
                for d in hw['disks']:
                    if not _same_disk(d['path'], parent) and d['free_bytes'] >= need[p['id']] + _margin(d['total_bytes']):
                        choice, folder, elsewhere = p, Path(d['path']), d
                        break
                if choice:
                    break
        if not choice:
            small = packs[-1]
            return {'pack': None, 'ai': False, 'folder': str(parent), 'need_bytes': need,
                    'why': f'The smallest library, {small["title"]}, needs {_gb(need[small["id"]])} free, and this computer has {_gb(free)} free.'}
        if elsewhere:
            home_free = free
            free, margin = elsewhere['free_bytes'], _margin(elsewhere['total_bytes'])
        left = free - need[choice['id']]
        ai = bool(model and hw['ai']['fits'] and left >= model['size'] + margin)
        if elsewhere:
            name = elsewhere.get('name') or Path(elsewhere['path']).name or elsewhere['path']
            why = (f'This computer’s own disk has {_gb(home_free)} free, not enough for any library, so {choice["title"]} goes on {name}. '
                   'Keep that disk connected: Lastbook reads the library from it.')
        else:
            why = 'Small is selected to get started. You can choose a larger library.'
        if self.default_why and not elsewhere and not self.current:
            why = self.default_why + ' ' + why
        if model and hw['ai']['fits'] and not ai:
            why += ' The local AI fits this computer’s memory, but not on the disk as well.'
        elif model and not hw['ai']['fits']:
            why += ' ' + hw['ai']['why']
        return {'pack': choice['id'], 'ai': ai, 'folder': str(folder), 'need_bytes': need, 'map_need_bytes': map_need, 'address_need_bytes': {x['id']: _map_need(x, ()) for x in self.manifest.current.get('address_indexes', []) if x['available']}, 'why': why}

    def state(self):
        self.manifest.fetch_once()
        hw = self.hardware()
        return {'mode': self.mode, 'hardware': hw, 'manifest': self.manifest.public(), 'current': self.current_info(),
                'found': self.found(), 'recommend': self.recommend(hw), 'job': self.public_job(), 'model_server': self.model_server()}

    # ------------------------------------------------------------------------- a model server on this computer (#78) --
    def model_server(self):
        """The server the person chose, as Setup shows it ({'address', 'model'}), or None. Asks the server nothing."""
        got = saved_model_server(self.settings_dir)
        return {'address': model_server.shown(got[0]), 'model': got[1]} if got else None

    def server_models(self, address):
        """The models of the server at the address the person typed: the one request Setup makes to it before a choice."""
        return model_server.list_models(address)

    def use_server(self, address, model):
        got = model_server.list_models(address)
        if got.get('error'):
            return got
        if model not in got['models']:
            return {'code': 'not_listed', 'error': f'The server at {got["address"]} no longer lists {str(model)[:model_server.MAX_ID]}. List its models again.'}
        self._choose_server(got['base'], model)
        return {'model_server': self.model_server()}

    def stop_server(self):
        self._choose_server(None, None)
        return {'model_server': None}

    def _choose_server(self, base, model):
        """Save the choice and switch the open library's assistant as one step, so the last choice to finish wins in both."""
        with self.choice_lock:
            with self.lock:
                s = self._settings()
                if base:
                    s['model_server'] = {'base': base, 'model': model}
                else:
                    s.pop('model_server', None)
                self._save_settings(s)
            if self.on_model_server:
                self.on_model_server(base, model)

    def public_job(self):
        with self.lock:
            if not self.job:
                return None
            job = json.loads(json.dumps({k: v for k, v in self.job.items() if k != 'plan'}))
            asked = self.job is self._cancelling
        if (asked or (job.get('error') or {}).get('code') == 'cancelled') and self._running():
            job['stopping'] = True      # cancelled, and its worker has not stopped: Setup shows "Finishing cancellation…" until it has
        now = time.time()
        for it in job['items']:      # retry_at becomes seconds from now, shown only while that wait is still ahead
            at = it.pop('retry_at', None)
            if at and it['state'] == 'downloading' and at > now:
                it['retry_in_s'] = round(at - now)
        return job

    # -------------------------------------------------------------------------------------------------- the actions --
    def _parent_for(self, folder):
        if folder:
            p = Path(folder).expanduser()
            return p.parent if p.name == DATA_NAME else p
        return self.current.parent if self.current else self.default_parent

    def start(self, pack_id=None, ai=False, folder=None, sources=None, explicit=False, repair=False, map_id=None, address_id=None, copy_to=None, check=False, as_is=False):
        if copy_to:
            return self._start_copy(copy_to)
        if repair or check:
            return self._start_repair(ai, check_only=check)
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            if self._running():
                return dict(STOPPING)
            pack = self.manifest.pack(pack_id) if pack_id else None
            if pack_id and (not pack or not pack['available']):
                return {'error': 'That library is not available yet.', 'code': 'unavailable'}
            cur = self.current_info() or {}
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None
            same_revision = pack and (not pack.get('revision') or pack['revision'] == cur.get('content_revision', ''))
            if pack and self.mode == 'manage' and cur.get('pack') == pack['id'] and same_revision and not folder:
                pack = None        # the same library again: only the AI is wanted
            model = self.manifest.model() if ai else None
            if ai and not model:
                return {'error': 'No local AI is offered for this version.', 'code': 'unavailable'}
            mp = self.manifest.map(map_id) if map_id else None
            if map_id and (not mp or not mp['available']):
                return {'error': 'That state’s map is not available yet.', 'code': 'unavailable'}
            requested_address = address_id
            if not as_is:      # a library zip used as it is (use()) brings no address index its pack's map would add
                address_id = address_id or map_id or (pack or {}).get('map') or cur.get('map')
            if address_id in ('base', 'us'):
                address_id = next(iter(cur.get('address_indexes', [])), None)
            addresses = self.manifest.address_index(address_id) if address_id else None
            if not addresses or not addresses['available']:
                if requested_address:
                    return {'error': 'That state’s address search is not available yet.', 'code': 'unavailable'}
                addresses = None
            if folder and self.current and install._real(Path(folder).expanduser()) == install._real(self.current):
                folder = None      # the window sends the shown folder, which in manage mode is the open library itself
            parent = self._parent_for(folder)
            target = self.current if self.current and not folder else parent / DATA_NAME   # the open library as it is, whatever its name
            parent = target.parent
            if addresses and addresses['id'] in cur.get('address_indexes', []) and target == self.current:
                addresses = None
            if model and cur.get('model') and target == self.current:
                model = None       # already here, and an upgrade in place carries it over
            if mp and not pack and cur.get('map') == mp['id']:
                mp = None          # this state's map is the one here already
            if self.mode == 'first-run' and not pack and (model or mp or addresses):
                return {'error': 'Choose a library first.', 'code': 'nothing'}
            if not pack and not model and not mp and not addresses:
                if self.mode == 'first-run':
                    return {'error': 'Choose a library first.', 'code': 'nothing'}
                return {'error': 'Nothing to add: this library and the local AI are already here.', 'code': 'nothing'}
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {'error': f'Lastbook cannot make a folder there ({type(e).__name__}).', 'code': 'folder'}
            if not os.access(parent, os.W_OK):
                return {'error': 'Lastbook cannot save in that folder. Choose another.', 'code': 'folder'}
            if path_too_deep(target):
                room = WINDOWS_MAX_PATH - LONGEST_INSIDE - len('\\') - len(DATA_NAME) - 1
                return {'error': f'That folder is too deep for Windows: the library’s longest file name would not fit. '
                                 f'Choose a folder whose path is at most {room} characters long, such as Documents.', 'code': 'folder'}
            if self.mode == 'first-run' and target.exists() and not install.is_library(target):
                if any(target.iterdir()):
                    return {'error': f'There is already a folder called {DATA_NAME} there that is not a library. Choose another place.', 'code': 'folder'}
            work = _work_for(target)
            srcs = dict(self.sources)
            srcs.update(sources or {})
            for f in self.found():
                if f.get('size_ok') and f['name'] not in srcs and f['kind'] in ('pack', 'model', 'map', 'addresses'):
                    srcs[f['name']] = f['path']
            pack = _pack_download(pack, work, srcs)
            split_files = pack['files'] if pack and pack.get('split_archive') else []
            names = ([f['name'] for f in pack['files']] if pack and not split_files else []) + ([model['file']] if model else []) + ([f['name'] for f in mp['files']] if mp else []) + ([f['name'] for f in addresses['files']] if addresses else [])
            have = set(srcs)
            if split_files:
                have_parts = self._on_disk({'pack': pack, 'work': work}, {'kind': 'pack'})
                need = install.need_bytes(None, model, have) + pack['download_bytes'] + pack['installed_bytes'] - have_parts
            else:
                need = install.need_bytes(pack, model, have)
            need += _map_need(mp, have) + _map_need(addresses, have)
            preserve_map = bool(pack and cur.get('pack') == pack['id'] and target == self.current and not mp and cur.get('map') not in (None, 'base'))
            if preserve_map:
                need += cur['map_bytes']
            for name in names:     # what an earlier run already downloaded is on the disk already
                if name in have:
                    continue
                for done in (work / 'downloads' / (name + '.part'), work / 'downloads' / name):
                    try:
                        need -= os.path.getsize(done)
                    except OSError:
                        pass
            free, _ = hardware.disk_of(parent)
            if free < need:
                return {'error': f'This needs {_gb(need)} free on that disk, and it has {_gb(free)}. Free some space or choose another disk.',
                        'code': 'no_space', 'need_bytes': need, 'free_bytes': free}
            items = []
            if pack:
                items.append({'id': pack['id'], 'kind': 'pack', 'title': pack['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': pack['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if mp:                 # after the pack: with a pack, the map replaces the pack's own map set in the staged library
                items.append({'id': mp['id'], 'kind': 'map', 'title': mp['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': mp['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if addresses:
                items.append({'id': addresses['id'], 'kind': 'addresses', 'title': addresses['title'] + ' address search', 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': addresses['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            if model:
                items.append({'id': model['id'], 'kind': 'model', 'title': model['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': model['size'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(),
                        'folder': str(target),
                        'plan': {'pack': pack, 'model': model, 'map': mp, 'addresses': addresses, 'parent': str(parent), 'target': str(target), 'work': str(work),
                                 'sources': {k: v for k, v in srcs.items() if k in names}, 'explicit': explicit,
                                 'retried': {}, 'switch': bool(pack)}}
            s = self._settings()
            s['work'] = str(work)
            self._save_settings(s)
            _tag_sources(self.job)
            self._save_job()
            self._run()
            return self.public_job()

    def _start_repair(self, ai, check_only=False):
        """Re-check the open library's files (pack-manifest.tsv) and the model's SHA-256. What is wrong is fetched again: the pack is
        downloaded and switched to as in an upgrade, the model replaced in place. Nothing is downloaded if all is well.
        check_only (Check now): the same check, and nothing is ever downloaded or changed; the result is kept (current_info 'checked')
        and the job's 'result' says what is damaged, so the person can choose Repair."""
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'A download is already under way. Pause or cancel it first.', 'code': 'busy'}
            if self._running():
                return dict(STOPPING)
            cur = self.current_info()
            if self.mode != 'manage' or not cur:
                return {'error': 'There is no library to check yet.' if check_only else 'There is no library to repair yet.', 'code': 'nothing'}
            pack = self.manifest.pack(cur['pack']) if cur['pack'] else None
            if not pack or not pack['available']:
                return {'error': f'Lastbook cannot tell which library this is, so it cannot {"check" if check_only else "repair"} it. Download a library instead.', 'code': 'unavailable'}
            # A model file in models/ is checked (and repaired) whatever its size: current_info() names a model only when its size
            # already matches, so a model one byte short would otherwise be skipped and the library called clean. A check looks only
            # at what is installed; a truly absent model stays "not installed".
            m = self.manifest.model()
            present = bool(m) and (self.current / 'models' / m['file']).is_file()
            model = m if (present if check_only else (ai or present)) else None
            parent, target = self.current.parent, self.current
            work = _work_for(target)
            srcs = {f['name']: f['path'] for f in self.found() if f.get('size_ok') and f['kind'] in ('pack', 'model', 'map')}
            pack = _pack_download(pack, work, srcs)
            items = [{'id': pack['id'], 'kind': 'pack', 'title': pack['title'], 'state': 'waiting', 'bytes_done': 0,
                      'bytes_total': pack['installed_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None}]
            if model:
                items.append({'id': model['id'], 'kind': 'model', 'title': model['title'], 'state': 'waiting', 'bytes_done': 0,
                              'bytes_total': model['size'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            mp, map_damaged = None, False
            if _separate_map(target) and not check_only:     # Check now checks the map in its worker, so a resumed check does it again
                map_damaged = _map_damaged(target, 'repair')
                if map_damaged:                               # Repair fetches the map again
                    mp = self.manifest.map(cur['map'])
                    if not mp or not mp['available']:
                        return {'error': f'The {cur["map"]} map in this library is damaged, and this version does not offer it to download again. '
                                         'Choose a map again.', 'code': 'unavailable'}
                    if mp:
                        items.insert(1, {'id': mp['id'], 'kind': 'map', 'title': mp['title'], 'state': 'waiting', 'bytes_done': 0,
                                         'bytes_total': mp['download_bytes'], 'rate_bps': 0, 'eta_s': None, 'error': None})
            names = [f['name'] for f in pack['files']] + ([model['file']] if model else []) + ([f['name'] for f in mp['files']] if mp else [])
            self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'items': items, 'error': None, 'started': _now(), 'folder': str(target),
                        'repair': True, 'check': check_only,
                        'plan': {'pack': pack, 'model': model, 'map': mp, 'parent': str(parent), 'target': str(target), 'work': str(work),
                                 'sources': {} if check_only else {k: v for k, v in srcs.items() if k in names}, 'explicit': False, 'retried': {},
                                 'switch': not check_only, 'repair': True, 'check_only': check_only}}
            if not check_only:
                self.job['plan']['map_damaged'] = map_damaged
                s = self._settings()      # Check now writes nothing beside the library, and leaves the work folder (and any
                s['work'] = str(work)     # download resumable there) to the job it belongs to
                self._save_settings(s)
            _tag_sources(self.job)
            self._save_job()
            self._run()
            return self.public_job()

    # ------------------------------------------------------------------------------------- copy for someone else (#114) --
    def copy_rows(self, lib):
        """What a copy holds: the library's own files as pack-manifest.tsv lists them (path, bytes, sha256), the manifest itself,
        and, where a state map replaced the pack's map, that map's files as map-set.json lists them (path, bytes, None). Nothing of
        the person's own is on either list: no settings, saved items, chats, notes, address indexes or local AI."""
        lib = Path(lib)
        rows = [l.split('\t') for l in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:] if l.strip()]
        rows = [(r[0], int(r[1]), r[2]) for r in rows]
        mp = install.map_set(lib)
        if mp:
            rows = [r for r in rows if not install.map_member(r[0])]
            rows += [(rel, int(size), None) for rel, size in sorted(mp['files'].items()) if install.map_member(rel)]
            rows.append(('map-set.json', (lib / 'map-set.json').stat().st_size, None))
        rows.append(('pack-manifest.tsv', (lib / 'pack-manifest.tsv').stat().st_size, None))
        root = lib.resolve()
        for rel, _, _ in rows:
            if '..' in Path(rel).parts or Path(rel).is_absolute():
                raise install.Refused('copy', f'The library lists a file outside itself ({rel[:80]!r}), so it was not copied.')
            real = (lib / rel).resolve()        # a link inside the library must not bring in a file from elsewhere (as library.real_path)
            if root not in real.parents:
                raise install.Refused('copy', f'The library’s {rel[:80]} leads outside the library, so nothing was copied.')
        return rows

    def _start_copy(self, where):
        """Copy the open library to <where>/LastbookData, for someone with no internet. Nothing is downloaded."""
        with self.lock:
            if self.job and self.job['state'] in ACTIVE + ('paused',):
                return {'error': 'Something else is under way. Let it finish, or cancel it, first.', 'code': 'busy'}
            if self._running():
                return dict(STOPPING)
            cur = self.current_info()
            if self.mode != 'manage' or not cur:
                return {'error': 'There is no library to copy yet.', 'code': 'nothing'}
            if _pending_file(self.settings_dir).exists():
                return {'error': 'An update is waiting for Lastbook to restart. Restart it, then copy.', 'code': 'busy'}
            p = Path(str(where or '')).expanduser()
            target = p if p.name == DATA_NAME else p / DATA_NAME
            here = self.current.resolve()
            try:
                there = target.resolve()
            except OSError:
                return {'error': 'Lastbook cannot use that place. Choose a drive or folder.', 'code': 'folder'}
            if not str(where or '').strip() or not target.parent.is_dir():
                return {'error': 'Lastbook cannot find that drive or folder.', 'code': 'missing'}
            if there == here or here in there.parents or there in here.parents:
                return {'error': 'Choose a place outside this library, such as a USB drive.', 'code': 'folder'}
            if target.is_symlink():
                return {'error': f'The {DATA_NAME} folder there is a link. Choose another drive or folder.', 'code': 'folder'}
            if target.exists() and not target.is_dir():
                return {'error': f'There is already a file called {DATA_NAME} there. Choose another drive or folder.', 'code': 'folder'}
            if target.exists() and any(target.iterdir()) and not (target / COPY_MARK).exists():
                return {'error': f'There is already a {DATA_NAME} folder there. Choose another drive or folder.', 'code': 'folder'}
            if not os.access(target.parent, os.W_OK):
                return {'error': 'Lastbook cannot save there. Choose a drive you can write to.', 'code': 'folder'}
            try:
                rows = self.copy_rows(self.current)
            except install.Refused as e:
                return {'error': e.message, 'code': 'copy'}
            except (OSError, ValueError, IndexError):
                return {'error': 'This library has no list of its files, so it cannot be copied. Check and repair it first.', 'code': 'unavailable'}
            total = sum(r[1] for r in rows)
            if max(r[1] for r in rows) >= FAT_MAX and (hardware.fs_type(target.parent) or '').lower() in hardware.FAT:
                return {'error': 'That drive cannot hold files larger than 4 GB (it is FAT32), and this library has some. Use a drive formatted as exFAT.',
                        'code': 'fat'}
            cluster = hardware.cluster_size(target.parent) or 128 * 1024   # unknown: assume exFAT's usual 128 KB
            on_disk = lambda n: -(-n // cluster) * cluster                     # every file takes whole clusters
            dirs = {str(Path(rel).parent) for rel, _, _ in rows}
            left = sum(on_disk(size) for rel, size, _ in rows if not ((target / rel).is_file() and (target / rel).stat().st_size == size))
            left += len(dirs) * cluster
            free, _ = hardware.disk_of(target.parent)
            if free < left + 64 * 2 ** 20:
                return {'error': f'This needs {_gb(left)} free, and that drive has {_gb(free)}. Choose a larger drive.', 'code': 'no_space',
                        'need_bytes': left, 'free_bytes': free}
            pack = self.manifest.pack(cur['pack']) if cur['pack'] else None
            title = (pack or {}).get('title') or cur['pack'] or 'Lastbook'
            self.job = {'id': uuid.uuid4().hex[:12], 'state': 'waiting', 'error': None, 'started': _now(), 'folder': str(target), 'copy': True,
                        'items': [{'id': 'copy', 'kind': 'copy', 'title': f'Copy of the {title} library', 'state': 'waiting', 'bytes_done': 0,
                                   'bytes_total': total, 'rate_bps': 0, 'eta_s': None, 'error': None}],
                        'plan': {'copy': True, 'source': str(self.current), 'target': str(target), 'work': str(target / COPY_MARK),
                                 'title': title, 'sources': {}, 'explicit': False, 'retried': {}, 'switch': False}}
            self._save_job()
            self._run()
            return self.public_job()

    def _do_copy(self, it, plan, control):
        """Each file to <target>/<path>.partial, then read back from the drive (past the system's cache where it can) and renamed
        only when what the drive gives back has the SHA-256 the list says, or, for a state map's files, the one read from the
        library. A drive that loses data (a fake-capacity stick) is caught here, not by the person with no internet.
        A file already there at its size and SHA-256 (an earlier copy that stopped) is kept. The source is never changed."""
        src, dst = Path(plan['source']), Path(plan['target'])
        rows = self.copy_rows(src)
        _dest_ok(dst, COPY_MARK)
        dst.mkdir(parents=True, exist_ok=True)
        with _open_new(dst / COPY_MARK) as f:
            f.write('Lastbook is copying a library here. Copy again to the same place to carry on.\n'.encode('utf-8'))
        tracker, done, total = self._track(it), 0, sum(r[1] for r in rows)
        self._set(it, 'copying', bytes_done=0, bytes_total=total)
        for rel, size, digest in rows:
            control.check()
            a, b = src / rel, dst / rel
            _dest_ok(dst, rel)
            if b.is_file() and b.stat().st_size == size and digest is not None and _read_back(b, control, lambda n: None) == digest:
                done += size
                tracker.progress(done, total, 'copying')
                continue
            if not a.is_file() or a.stat().st_size != size:
                raise install.Refused('copy', f'The library’s {rel} is missing or the wrong size, so the copy stopped. Check and repair the library, then copy again.')
            b.parent.mkdir(parents=True, exist_ok=True)
            if not _inside(dst, b.parent):           # checked again now the folders exist
                raise install.Refused('copy', f'On the drive, {rel} would land outside the copy, so nothing more was written. Choose another drive or folder.')
            part = b.with_name(b.name + '.partial')
            plan['partial'] = str(part)
            h, base, written = hashlib.sha256(), done, 0
            try:
                with open(a, 'rb') as fa, _open_new(part) as fb:
                    while True:
                        control.check()
                        chunk = fa.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        fb.write(chunk)
                        h.update(chunk)
                        written += len(chunk)
                        tracker.progress(base + written // 2, total, 'copying')    # writing is half the work, reading it back the other
                    fb.flush()
                    os.fsync(fb.fileno())
                    _drop_cache(fb.fileno())
            except OSError as e:
                too_big = size > FAT_MAX and written >= FAT_MAX - 64 * 2 ** 20
                if too_big and e.errno in (errno.EFBIG, errno.EINVAL, errno.ENOSPC):   # Windows says "disk full" at FAT's 4 GB limit
                    raise install.Refused('copy', 'That drive cannot hold files larger than 4 GB (it is probably FAT32). Use a drive formatted as exFAT.')
                raise
            source = h.hexdigest()
            if digest is not None and source != digest:
                part.unlink()
                raise install.Refused('copy', f'The library’s {rel} does not match its published copy, so it was not copied. Check and repair the library, then copy again.')
            back = _read_back(part, control, lambda n: tracker.progress(base + (size + n) // 2, total, 'copying'))
            if back != source:
                part.unlink()
                raise install.Refused('copy', f'The drive gave back different bytes for {rel} than were written to it. It may be faulty, or smaller '
                                              'than it claims. Use another drive.')
            os.replace(part, b)
            plan.pop('partial', None)
            done = base + size
        plan['copied'] = {'files': len(rows), 'bytes': total}

    def _finish_copy(self, plan):
        """The drive's START-HERE.txt, then the copy counts as complete."""
        dst = Path(plan['target'])
        c = plan.get('copied') or {}
        text = START_HERE.format(title=plan.get('title') or 'Lastbook', size=_gb(c.get('bytes') or 0), files=c.get('files') or 0,
                                 date=time.strftime('%Y-%m-%d'), data=DATA_NAME)
        names = ['START-HERE.txt', 'START-HERE (Lastbook).txt'] + [f'START-HERE (Lastbook {n}).txt' for n in range(2, 100)]
        name = next((n for n in names if not (dst.parent / n).is_symlink() and (not (dst.parent / n).exists() or _ours(dst.parent / n))), None)
        if name is None:
            raise install.Refused('copy', 'The drive already holds many START-HERE files that are not Lastbook’s. Remove some, then copy again.')
        tmp = dst.parent / f'.{name}.{uuid.uuid4().hex[:8]}.partial'      # a fresh name: never someone else's file, never a link
        with _open_new(tmp) as f:
            f.write(text.replace('\n', '\r\n').encode('utf-8'))       # Windows line ends: every computer's text editor opens it
        os.replace(tmp, dst.parent / name)        # someone else's file of that name is never replaced: name is new or ours
        try:
            (dst / COPY_MARK).unlink()
        except FileNotFoundError:
            pass
        self.job['result'] = {'files': c.get('files'), 'bytes': c.get('bytes'), 'folder': str(dst), 'start_here': name}

    def _check_library(self, it, lib, control):
        """Files of the open library that are missing or differ from its pack-manifest.tsv (path, bytes, sha256)."""
        try:
            rows = [l.split('\t') for l in (lib / 'pack-manifest.tsv').read_text(encoding='utf-8').splitlines()[1:] if l.strip()]
            rows = [(r[0], int(r[1]), r[2]) for r in rows]
        except (OSError, ValueError, IndexError):
            return ['pack-manifest.tsv']
        if _separate_map(lib):        # a state map replaced the pack's own: _start_repair checks it against map-set.json instead
            rows = [r for r in rows if not install.map_member(r[0])]
        total = sum(r[1] for r in rows) or 1
        tracker, done, bad = self._track(it), 0, []
        self._set(it, 'verifying', bytes_done=0, bytes_total=total)
        for rel, size, digest in rows:
            p = lib / rel
            if '..' in Path(rel).parts or Path(rel).is_absolute() or not p.is_file() or p.stat().st_size != size:
                bad.append(rel)
                done += size
                tracker.progress(done, total, 'verifying')
                continue
            base = done
            if _sha256(p, control, lambda d, t: tracker.progress(base + d, total, 'verifying')) != digest:
                bad.append(rel)
            done = base + size
        return bad

    def _running(self):
        """A worker thread is still alive (a cancelled one that outlived cancel()'s join, say): it may still be writing into the work
        folder, so no new job starts until it has stopped."""
        t = self.thread
        return bool(t and t.is_alive() and t is not threading.current_thread())

    def _run(self):
        if self._stages(self.job):
            self._drop_deferred_switch()
        self.control = download.Control()
        self._run_id += 1
        self.thread = threading.Thread(target=self._work, args=(self.control, self._run_id), daemon=True, name='setup-job')
        self.thread.start()

    def _drop_deferred_switch(self, keep_for=None):
        """A switch refused at a restart and kept for the next (its pending file counts `attempts`) is dropped when a job would
        replace what it uses (a library or map download rewrites the unpack and the pending file), or when the person chooses
        another library (keep_for: the one chosen; its staged unpack goes too, as nothing will use it). A job adding only the
        AI or addresses keeps it, and so does a switch staged as usual."""
        pend = _pending_file(self.settings_dir)
        try:
            d = json.loads(pend.read_text(encoding='utf-8'))
            if 'attempts' in d and not (keep_for and install._real(d['target']) == install._real(keep_for)):
                pend.unlink()
                if keep_for:
                    shutil.rmtree(_work_for(Path(d['target'])) / 'unpack', ignore_errors=True)
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _stages(job):
        """A library or map download stages a switch (the unpack and the pending file); a job adding only the AI or addresses
        does not, and leaves a switch already staged alone."""
        return bool(job['plan'].get('pack') or job['plan'].get('map'))

    def pause(self):
        with self.lock:
            if self.job and self.job['state'] in ACTIVE and self.control:
                self.control.stop('paused')
        self._join()
        return self.public_job()

    def resume(self):
        with self.lock:
            if self._running() and not (self.job and self.job['state'] in ACTIVE):   # already running (another window): show it
                return dict(STOPPING)
            if self.job and self.job['state'] == 'paused':
                self.job['state'] = 'waiting'
                for it in self.job['items']:
                    it.pop('paused_from', None)
                self._save_job()
                self._run()
        return self.public_job()

    def retry(self):
        with self.lock:
            if self._running() and not (self.job and self.job['state'] in ACTIVE):   # already running (another window): show it
                return dict(STOPPING)
            if self.job and self.job['state'] == 'failed':
                self.job['state'], self.job['error'] = 'waiting', None
                self.job['plan']['retried'] = {}
                for it in self.job['items']:
                    if it['state'] != 'done':
                        it['state'], it['error'] = 'waiting', None
                self._save_job()
                self._run()
        return self.public_job()

    def cancel(self):
        with self.lock:
            job = self.job
            if not job:
                return None
            if job['state'] in ACTIVE and self.control:
                self.control.stop('cancelled')
            self._run_id += 1       # a worker that outlives the join is stale for good, even if retry() or resume() reuse this job
            self._cancelling = job
        self._join()
        with self.lock:
            self._cancelling = None
            if job['plan'].get('check_only'):     # a check made nothing: no work folder, download or staged update is its to clear
                if job['state'] != 'done':
                    job['state'] = 'failed'
                    job['error'] = {'code': 'cancelled', 'message': 'Cancelled. Nothing was changed.'}
                    for it in job['items']:
                        if it['state'] != 'done':
                            it['state'] = 'failed'
                    self._save_job()
            elif job.get('copy') and job['state'] != 'done':   # what is copied stays on the drive: copying there again carries on
                part = job['plan'].get('partial')       # kept: a worker that outlived the join may make it yet, and clears it as it ends
                if part:
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
                job['state'] = 'failed'
                job['error'] = {'code': 'cancelled', 'message': 'Cancelled. This library was not changed; copy again to the same place to carry on.'}
                for it in job['items']:
                    if it['state'] != 'done':
                        it['state'] = 'failed'
                self._save_job()
            elif job['state'] != 'done' or job['plan'].get('switch'):     # a staged library not yet switched to can still be dropped
                self._clean(job)
                if self._stages(job):
                    try:
                        _pending_file(self.settings_dir).unlink()
                    except OSError:
                        pass
                job['state'] = 'failed'
                job['error'] = {'code': 'cancelled', 'message': FINISHING if self._running() else 'Cancelled. Nothing was changed.'}
                for it in job['items']:
                    if it['state'] != 'done':
                        it['state'] = 'failed'
                self._save_job()
        return self.public_job()

    def _join(self):
        t = self.thread
        if t and t is not threading.current_thread():
            t.join(30)

    def _clean(self, job):
        """What the job made in its work folder goes; the person's own files and any library stay."""
        work = Path(job['plan']['work'])
        try:
            _check_pack_paths(work)
        except (download.Failed, OSError):
            return  # Cancel still completes when the work folder is linked or its drive is unavailable.
        subs = ('downloads', 'unpack', 'unpack-map', 'unpack-addresses') if self._stages(job) else ('downloads', 'unpack-addresses')
        for sub in subs:
            shutil.rmtree(work / sub, ignore_errors=True)

    def use(self, path, library_only=False, folder=None):
        """A library or file already on this computer. library_only (Library and local AI's "Open another library folder", #266):
        only an existing library, or the folder that holds one, is accepted. A folder holding a pack zip, or a file, is refused
        and nothing starts, so choosing a folder can never begin replacing the open library. folder: the place chosen on screen;
        a library zip is installed there, as it is, with no download (the AI, a map or addresses are added after, as a choice)."""
        p = Path(path or '').expanduser()
        if not str(path or '').strip() or not p.exists():
            return {'error': 'Lastbook cannot find that file or folder.', 'code': 'missing'}
        if library_only and not p.is_dir():
            return {'error': f'Choose a library folder, the one called {DATA_NAME}, not a file.', 'code': 'not_library'}
        if p.is_dir():
            lib = p if install.is_library(p) else p / DATA_NAME
            if library_only and not install.is_library(lib):
                return {'error': f'“{p.name}” is not a Lastbook library. Choose the folder called {DATA_NAME}.', 'code': 'not_library'}
            if not install.is_library(lib):
                known = {f['name'] for pk in self.manifest.current['packs'] for f in pk['files']}
                model = self.manifest.model()
                if model:
                    known.add(model['file'])
                inside = sorted(c for c in p.iterdir() if c.name in known and c.is_file()) if p.is_dir() else []
                if inside:          # the person explicitly chose this folder
                    return self.use(str(inside[0]), folder=folder)
                return {'error': f'“{p.name}” is not a Lastbook library. Choose the folder called {DATA_NAME}.', 'code': 'not_library'}
            if self.current and install._real(lib) == install._real(self.current):
                return {'error': 'That library is the one open now.', 'code': 'current'}
            if not os.access(lib, os.W_OK):
                return {'error': f'Lastbook cannot save in “{lib.name}”. Move it somewhere you can save to, then choose it there.', 'code': 'folder'}
            with self.lock:
                if self.job and self.job['state'] in ACTIVE:
                    return {'error': 'A download is under way. Pause or cancel it first.', 'code': 'busy'}
                self._drop_deferred_switch(keep_for=lib)
                self.job = {'id': uuid.uuid4().hex[:12], 'state': 'done', 'error': None, 'started': _now(), 'folder': str(lib),
                            'items': [{'id': 'library', 'kind': 'pack', 'title': lib.name, 'state': 'done', 'bytes_done': 0, 'bytes_total': 0,
                                       'rate_bps': 0, 'eta_s': None, 'error': None}],
                            'plan': {'use_library': str(lib), 'work': str(_work_for(lib)), 'target': str(lib)}}
                return self.public_job()
        name = p.name
        for pk in self.manifest.current['packs']:
            for f in pk['files']:
                if f['name'] == name:
                    if p.stat().st_size != f['size']:
                        return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(f["size"])} of the published {name}. The download may not have finished.', 'code': 'size'}
                    if not pk['available']:
                        return {'error': 'That library is not available in this version.', 'code': 'unavailable'}
                    self.sources[name] = str(p)
                    return self.start(pk['id'], False, folder, {name: str(p)}, explicit=True, as_is=True)
        for x in self.manifest.current.get('maps', []):
            for f in x['files']:
                if f['name'] == name:
                    if p.stat().st_size != f['size']:
                        return {'error': f'That file is {_gb(p.stat().st_size)}, not the {_gb(f["size"])} of the published {name}. The download may not have finished.', 'code': 'size'}
                    self.sources[name] = str(p)
                    if self.mode == 'manage':
                        return self.start(None, False, None, {name: str(p)}, explicit=True, map_id=x['id'])
                    return self.public_job() or {'job': None, 'noted': True, 'kind': 'map'}
        for entry in self.manifest.current.get('address_indexes', []):
            for file in entry['files']:
                if file['name'] != name:
                    continue
                if p.stat().st_size != file['size']:
                    return {'error': 'The address index download is incomplete.', 'code': 'size'}
                self.sources[name] = str(p)
                if self.mode == 'manage':
                    return self.start(sources={name: str(p)}, explicit=True, address_id=entry['id'])
                return self.public_job() or {'job': None, 'noted': True, 'kind': 'addresses'}
        m = self.manifest.model()
        if m and name == m['file']:
            if p.stat().st_size != m['size']:
                return {'error': f'That file is {p.stat().st_size:,} bytes, not the {m["size"]:,} of the model. The download may not have finished.', 'code': 'size'}   # exact: a nearly finished 17 GB download rounds to the same GB
            self.sources[name] = str(p)
            with self.lock:
                job = self.job
                code = (job['error'] or {}).get('code') if job else None
                # The same cases setup.js offers Choose AI model file for: a stop as 'unreachable' (also with part of the model
                # already downloaded, from an earlier try), or a 'network' stop before any byte of it came.
                if (job and job['state'] == 'failed' and code in ('network', 'unreachable')
                        and (job['plan'].get('model') or {}).get('file') == name
                        and any(it['kind'] == 'model' and it['state'] == 'failed' and (code == 'unreachable' or not it['bytes_done'])
                                for it in job['items'])):
                    job['plan']['sources'][name] = str(p)
                    job['plan']['verified'] = [n for n in job['plan'].get('verified', []) if n != name]
                    job['plan']['explicit'] = True
                    _tag_sources(job)
                    return self.retry()  # keep the finished pack; check the chosen model before using it
            if self.mode == 'manage':
                return self.start(None, True, None, {name: str(p)}, explicit=True)
            return self.public_job() or {'job': None, 'noted': True, 'kind': 'model'}
        return {'error': 'That is not a file Lastbook knows. Choose a Lastbook library zip or the model file.', 'code': 'unknown'}

    def finish(self):
        with self.lock:
            job = self.job
            if not job or job['state'] != 'done':
                return {'error': 'Setup has not finished yet.', 'code': 'not_done'}
            if job.get('check'):      # a check changed nothing: there is nothing to restart into
                return {'error': 'A check changes nothing, so there is nothing to open.', 'code': 'nothing'}
            if job.get('copy'):       # a copy went to a drive: there is no library here to switch to
                return {'error': 'A copy has no library to open here. It is on the drive.', 'code': 'nothing'}
            plan = job['plan']
            folder = plan.get('use_library') or plan['target']
        if not self.on_finish:
            return {'ok': True, 'restart': True, 'folder': folder}
        threading.Timer(0.4, self.on_finish, [folder]).start()     # after this answer has reached the window
        return {'ok': True, 'folder': folder}

    def pick(self, files=False):
        """{path, free_bytes, disk}, or {path: null, cancelled: true}."""
        path = None
        if self.pick_folder:
            try:
                path = (self.pick_folder(files=True) if files else self.pick_folder()) or None
            except Exception:
                traceback.print_exc()
        if not path:
            return {'path': None, 'cancelled': True}
        try:
            free, _ = hardware.disk_of(path)
        except OSError:
            free = None
        return {'path': path, 'free_bytes': free, 'disk': hardware.disk_name(path)}

    def remove_ai(self):
        """Manage mode: the model file goes from the open library. The library itself is not touched."""
        m = self.manifest.model()
        if self.mode != 'manage' or not self.current or not m:
            return {'error': 'There is no local AI in this library to remove.', 'code': 'nothing'}
        with self.lock:
            if self.job and self.job['state'] in ACTIVE:
                return {'error': 'A download is under way. Pause or cancel it first.', 'code': 'busy'}
            if self._running():
                return dict(STOPPING)
        p = self.current / 'models' / m['file']
        if not p.is_file():
            return {'error': 'There is no local AI in this library to remove.', 'code': 'nothing'}
        llm = self.llm()
        local = getattr(llm, 'LOCAL', None) if llm else None
        try:
            if local is not None and hasattr(local, 'stop'):
                local.stop('removed')
        except Exception:
            traceback.print_exc()
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError as e:
            return {'error': f'Lastbook could not remove the model file ({type(e).__name__}). Quit anything using it, then try again.', 'code': 'folder'}
        if local is not None and getattr(local, '_started', False):
            threading.Thread(target=local.discover, daemon=True).start()
        return {'ok': True, 'freed_bytes': size}

    def delete_old(self, path):
        s = self._settings()
        olds = s.get('old', [])
        if path not in olds:
            return {'error': 'Lastbook only deletes an old library it moved aside itself.', 'code': 'unknown'}
        p = Path(path)
        if '.old-' not in p.name:
            return {'error': 'That is not an old library.', 'code': 'unknown'}

        def gone():
            shutil.rmtree(p, ignore_errors=True)
            s2 = self._settings()
            s2['old'] = [o for o in s2.get('old', []) if o != path or Path(o).exists()]
            self._save_settings(s2)
            self.deleting.discard(path)
        self.deleting.add(path)
        threading.Thread(target=gone, daemon=True, name='setup-delete-old').start()
        return {'ok': True, 'deleting': path}

    # ------------------------------------------------------------------------------------------------------ the work --
    def _stale(self):
        """True in a worker that is no longer the job's: cancelled (and not joined in time) or replaced by a newer run."""
        run = getattr(self._worker, 'run', None)
        return run is not None and (run != self._run_id or self._worker.job is not self.job)

    def _track(self, it):
        return _Item(it, self._stale)

    def _set(self, it, state, **kw):
        with self.lock:
            if self._stale():
                raise download.Stopped('cancelled')       # a late worker writes nothing
            if it is not None:
                it['state'] = state
                it.update(kw)
            self.job['state'] = state
            self._save_job()

    def _fail(self, it, code, message, **extra):
        with self.lock:
            if self._stale():
                return
            err = {'code': code, 'message': message, **extra}
            if it is not None:
                it['state'], it['error'], it['rate_bps'], it['eta_s'] = 'failed', err, 0, None
            self.job['state'], self.job['error'] = 'failed', err
            self._save_quietly()
        install.say(f'setup: failed ({code}): {message}')

    def _save_quietly(self):
        try:
            self._save_job()
        except OSError as e:
            print('setup: could not save the job:', e, flush=True)

    def _work(self, control, run):
        job, self._worker.job, self._worker.run = self.job, self.job, run
        plan = job['plan']
        it = None
        try:
            work = Path(plan['work'])
            if not plan.get('copy') and not plan.get('check_only'):     # a copy writes only to its drive, a check nothing at all
                if (plan.get('pack') or {}).get('split_archive'):
                    _check_pack_paths(work, work / 'downloads', _pack_parts_folder(work), work / 'unpack')
                (work / 'downloads').mkdir(parents=True, exist_ok=True)
            elif plan.get('check_only') and 'map_damaged' not in plan:   # Check now: a state map against its map-set.json, from the files as they are now
                plan['map_damaged'] = _separate_map(Path(plan['target'])) and _map_damaged(Path(plan['target']), 'check')
            for it in job['items']:
                if it['state'] == 'done':
                    continue
                if it['kind'] == 'copy':
                    self._do_copy(it, plan, control)
                elif it['kind'] == 'pack':
                    self._do_pack(it, plan, work, control)
                elif it['kind'] == 'map':
                    self._do_map(it, plan, work, control)
                elif it['kind'] == 'addresses':
                    self._do_addresses(it, plan, work, control)
                elif plan.get('check_only'):
                    self._check_model(it, plan, control)
                else:
                    self._do_model(it, plan, work, control)
                with self.lock:
                    if self._stale():
                        raise download.Stopped('cancelled')
                    it['state'], it['rate_bps'], it['eta_s'] = 'done', 0, None
                    self._save_job()
            it = None
            self._finish_job(plan, work)
        except download.Stopped as s:
            with self.lock:
                if self._stale():
                    return          # a late worker writes nothing
                if s.why == 'paused':
                    if it is not None:
                        it['state'], it['rate_bps'], it['eta_s'] = 'paused', 0, None
                    job['state'] = 'paused'
                self._save_quietly()
        except download.Failed as e:
            self._fail(it, e.code, e.message, **e.extra)
        except install.Refused as e:
            self._fail(it, e.code, e.message)
        except OSError as e:
            if plan.get('check_only'):  # a file it could not read, not the internet: a check downloads nothing
                traceback.print_exc()
                self._fail(it, 'check', f'Lastbook could not read one of the files it was checking ({type(e).__name__}: {e.strerror or e}).')
            elif plan.get('copy'):      # the drive, not the internet: a copy downloads nothing
                traceback.print_exc()
                if download.disk_full(e):
                    self._fail(it, 'no_space', 'The drive filled up. What was copied is kept: use a larger drive, or free some space and copy again to the same place.')
                else:
                    self._fail(it, 'copy', f'Lastbook could not read or write a file for the copy ({type(e).__name__}: {e.strerror or e}). '
                                           'Check the drive is still connected, then copy again to the same place to carry on.')
            elif download.disk_full(e):
                self._fail(it, 'no_space', 'The disk filled up. Free some space, then try again.')
            else:
                traceback.print_exc()
                self._fail(it, 'unpack' if it and job['state'] == 'unpacking' else 'network', f'Lastbook could not write a file ({type(e).__name__}: {e.strerror or e}).')
        except Exception as e:
            traceback.print_exc()
            self._fail(it, 'activate', f'Setup stopped on an internal error ({type(e).__name__}).')
        finally:
            if self._stale():
                self._finish_cancel(job)

    def _finish_cancel(self, job):
        """A worker that outlived cancel()'s join, on its way out: what it wrote after cancel() cleared the work folder goes too,
        before the thread ends, so Retry (refused while it is alive) never meets it."""
        with self.lock:
            if (job.get('error') or {}).get('code') != 'cancelled' or job['plan'].get('check_only'):
                return
            if job.get('copy'):
                part = job['plan'].pop('partial', None)
                if part:
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
                return
            self._clean(job)
            if job is self.job and job['error'].get('message') == FINISHING:
                job['error'] = {'code': 'cancelled', 'message': 'Cancelled. Nothing was changed.'}
                self._save_job()

    def _get_file(self, it, f, plan, work, control, offset, *, part=False):
        """The file, checked against its SHA-256: one already on this computer, or a download. Returns its path."""
        tracker = self._track(it)
        name = f['name']
        src = None if part else plan['sources'].get(name)
        if src and os.path.isfile(src) and name in plan.setdefault('verified', []):
            return src, False
        explicit_use = bool(plan.get('explicit'))
        if src and os.path.isfile(src):
            self._set(it, 'verifying')
            digest = _sha256(src, control, lambda d, t: tracker.progress(offset + d, it['bytes_total'], 'verifying'))
            if digest == f['sha256']:
                plan['verified'].append(name)
                return src, False
            st = os.stat(src)
            self.rejected[src] = (st.st_size, st.st_mtime_ns)
            plan['sources'].pop(name, None)       # a retry downloads it instead; the file itself is left where it is, untouched
            it['source'] = 'download'
            plan['explicit'] = False
            if explicit_use:
                folder = Path(src).parent.name or str(Path(src).parent)
                raise download.Failed('found_mismatch', f'The {name} in {folder} doesn’t match the published file, so it was not used. '
                                                        'It was left as it is. Download it instead?', file=src)
            print(f'setup: {src} does not match; downloading instead', flush=True)
        dest = (_pack_parts_folder(work) if part else work / 'downloads') / name
        if part:
            _check_pack_paths(*(dest.with_name(name + suffix) for suffix in ('', '.part', '.part.json', '.part.json.tmp')))
            if dest.is_file():
                if it['state'] != 'verifying':
                    self._set(it, 'verifying')
                if dest.stat().st_size == f['size'] and _sha256(
                    dest, control, lambda d, t: tracker.progress(offset + d, it['bytes_total'], 'verifying')
                ) == f['sha256']:
                    return dest, True
                dest.unlink()
        if not dest.exists():       # room for what is still to come (the space check at start counted a file already here as had)
            try:
                still = f['size'] - os.path.getsize(str(dest) + '.part')
            except OSError:
                still = f['size']
            free, _ = hardware.disk_of(work)
            if free < still + 512 * 2 ** 20:
                raise download.Failed('no_space', f'Downloading {name} needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.',
                                      need_bytes=still, free_bytes=free)
        while True:
            if not part or it['state'] != 'downloading':
                self._set(it, 'downloading')
            d = download.Download(f['url'], dest, f['size'], f['sha256'], control,
                                  lambda done, total, phase: tracker.progress(offset + done, it['bytes_total'], phase),
                                  allowed=self.allowed, context=self.context, waiting=tracker.waiting)
            try:
                return d.run(), True
            except download.BadChecksum:
                tries = plan['retried'].get(name, 0)
                if tries >= 1:
                    raise download.Failed('checksum', f'{name} did not match its published checksum twice. The file on the server may be damaged; try again later.')
                plan['retried'][name] = tries + 1
                print(f'setup: {name} failed its checksum; downloading it once more', flush=True)
            except download.Failed as e:
                if e.code == 'unreachable':       # what the person needs to get the file another way, then choose it (use)
                    e.extra.update(name=name, size=f['size'], url=f['url'], sha256=f['sha256'])
                raise

    def _do_pack(self, it, plan, work, control):
        pack = plan['pack']
        if plan.get('repair') and 'damaged' not in plan:
            bad = self._check_library(it, Path(plan['target']), control)
            plan['damaged'] = bad
            print(f'setup: {"check" if plan.get("check_only") else "repair"} found {len(bad)} damaged or missing files', flush=True)
            if plan.get('check_only'):     # Check now: report, never download
                plan['switch'] = False
                return
            if not bad:
                plan['switch'] = False
                return
            need = install.need_bytes(pack, None, set(plan['sources']))
            free, _ = hardware.disk_of(work.parent)
            if free < need:
                raise download.Failed('no_space', f'{len(bad)} files need fetching again. That needs {_gb(need)} free, and the disk has {_gb(free)}.')
            with self.lock:
                it['bytes_total'], it['bytes_done'] = pack['download_bytes'], 0
        staged = work / 'unpack'
        if pack.get('split_archive'):
            _check_pack_paths(staged / 'unpacked-from.json', staged / 'unpacked-from.json.tmp')
        archive_hashes = [pack['split_archive']] if pack.get('split_archive') else [f['sha256'] for f in pack['files']]
        _fresh_unpack(staged, {'pack': pack['id'], 'revision': pack.get('revision'), 'files': archive_hashes})
        got, offset = [], 0
        split = bool(pack.get('split_archive'))
        if split:
            with self.lock:
                it['bytes_total'] = pack['download_bytes']
                it['bytes_done'] = self._on_disk(plan, it)
        for f in pack['files']:
            path, ours = self._get_file(it, f, plan, work, control, offset, part=split)
            got.append((path, ours))
            offset += f['size']
        self._set(it, 'unpacking', bytes_done=0, bytes_total=pack['installed_bytes'])
        paths = [path for path, _ in got]
        if split:
            with io.BufferedReader(install.SplitZip(paths, (f['size'] for f in pack['files']))) as archive:
                lib = self._unpack_pack(it, staged, pack['installed_bytes'], control, [archive])
        else:
            lib = self._unpack_pack(it, staged, pack['installed_bytes'], control, paths)
        if pack.get('revision'):
            metadata = json.loads((lib / 'pack.json').read_text(encoding='utf-8'))
            if metadata.get('content_revision') != pack['revision']:
                raise install.Refused('unpack', 'The library content version does not match this download.')
        current = self.current_info() or {}
        if (not plan.get('map') and Path(plan['target']) == self.current and current.get('pack') == pack['id']
                and _separate_map(self.current)):          # kept only when Repair found it whole, or it would have its own item
            install.copy_map(self.current, lib)
        install._write_json(lib / 'setup-pack.json', {'pack': pack['id'], 'installed': _now(), 'files': [f['name'] for f in pack['files']]})
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _unpack_pack(self, it, staged, installed_bytes, control, archives):
        tracker = self._track(it)
        total = sum(install.check_zip(z, installed_bytes)[1] for z in archives)    # all members checked before any is written
        still = max(total - _tree_size(staged), 0)
        free, _ = hardware.disk_of(staged.parent)
        if free < still + 512 * 2 ** 20:
            raise download.Failed('no_space', f'Unpacking needs {_gb(still)} free, and the disk has {_gb(free)}. Free some space, then try again.')
        for z in archives:
            lib = install.unpack(z, staged, installed_bytes, control, lambda d, t: tracker.progress(d, t, 'unpacking'))
        return lib

    def _do_map(self, it, plan, work, control):
        """A state map: download, check, unpack (map files only) into unpack-map/. With a pack, it replaces the staged library's own
        map set at once; on its own it is staged and switched in when the app restarts (install._apply_map)."""
        mp = plan['map']
        got, offset = [], 0
        for f in mp['files']:
            path, ours = self._get_file(it, f, plan, work, control, offset)
            got.append((path, ours))
            offset += f['size']
        tracker = self._track(it)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=mp['installed_bytes'])
        staged = work / 'unpack-map'
        _fresh_unpack(staged, {'map': mp['id'], 'files': [f['sha256'] for f in mp['files']]}, keep=False)
        for path, _ in got:
            install.check_zip(path, mp['installed_bytes'], install.map_member)
        for path, _ in got:
            lib = install.unpack(path, staged, mp['installed_bytes'], control, lambda d, t: tracker.progress(d, t, 'unpacking'), allowed=install.map_member)
        if install.map_name(lib) != mp['id']:
            raise install.Refused('unpack', f'The map file says it is {install.map_name(lib)}, not {mp["id"]}.')
        if plan['switch']:
            install.replace_map_in(work / 'unpack' / DATA_NAME, lib)
            shutil.rmtree(staged, ignore_errors=True)
        else:
            plan['map_new'] = str(lib)
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _do_addresses(self, it, plan, work, control):
        entry = plan['addresses']
        relative = 'derived/geo/addresses/' + entry['id'] + '.sqlite'
        directories = {'', 'derived', 'derived/geo', 'derived/geo/addresses'}

        def allowed(name, is_dir=False):
            return name in directories if is_dir else name == relative

        def verify(lib):
            path = lib / relative
            if not path.is_file() or path.stat().st_size != entry['installed_bytes']:
                raise install.Refused('unpack', 'The address index is missing or the wrong size.')

        got = []
        offset = 0
        for file in entry['files']:
            path, ours = self._get_file(it, file, plan, work, control, offset)
            got.append((path, ours))
            offset += file['size']
        for path, _ in got:
            install.check_zip(path, entry['installed_bytes'], allowed)
        _fresh_unpack(work / 'unpack-addresses', {'addresses': entry['id'], 'files': [f['sha256'] for f in entry['files']]}, keep=False)
        self._set(it, 'unpacking', bytes_done=0, bytes_total=entry['installed_bytes'])
        tracker = self._track(it)
        for path, _ in got:
            lib = install.unpack(path, work / 'unpack-addresses', entry['installed_bytes'], control,
                                 lambda done, total: tracker.progress(done, total, 'unpacking'), allowed=allowed, verify=verify)
        target = work / 'unpack' / DATA_NAME if plan['switch'] else Path(plan['target'])
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        os.replace(lib / relative, destination)
        install._write_json(destination.with_suffix('.json'), {'files': {file['name']: file['sha256'] for file in entry['files']}})
        for path, ours in got:
            if ours:
                os.unlink(path)

    def _check_model(self, it, plan, control):
        """Check now: the installed model file against its published size and SHA-256, where it is. Nothing is fetched or moved."""
        m = plan['model']
        p = Path(plan['target']) / 'models' / m['file']
        tracker = self._track(it)
        self._set(it, 'verifying', bytes_done=0, bytes_total=m['size'])
        ok = p.is_file() and p.stat().st_size == m['size']
        if ok:
            ok = _sha256(p, control, lambda d, t: tracker.progress(d, t, 'verifying')) == m['sha256']
        plan['model_ok'] = ok

    def _do_model(self, it, plan, work, control):
        m = plan['model']
        path, ours = self._get_file(it, {'name': m['file'], 'url': m['url'], 'size': m['size'], 'sha256': m['sha256']}, plan, work, control, 0)
        lib = (work / 'unpack' / DATA_NAME) if plan['switch'] else Path(self.current or plan['target'])
        dest = lib / 'models' / m['file']
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._set(it, 'activating')
        src = path
        if not (ours or _same_disk(path, dest)):
            src = dest.with_name(dest.name + '.partial')
            shutil.copyfile(path, src)        # minutes for a large model: cancel() may have given up on this worker meanwhile
        with self.lock:
            if self._stale():                 # cancelled while copying: the open library does not get it
                if src != path:
                    try:
                        os.unlink(src)
                    except OSError:
                        pass
                raise download.Stopped('cancelled')
            os.replace(src, dest)
        plan['model_file'] = str(Path(plan['target']) / 'models' / m['file']) if plan['switch'] else str(dest)
        plan['model_sha256'] = m['sha256']           # what _get_file just matched: the streamed or re-read hash equalled this
        if plan.get('repair'):
            plan['model_ok'] = not ours              # Repair downloaded it only if the installed one was damaged or missing
        if not plan['switch'] and self.current:      # the library open now: the AI can use it at once
            self._mark_checked(dest, m['sha256'])

    def _mark_checked(self, path, sha256):
        llm = self.llm()
        if not llm:
            return
        try:
            llm.mark_checked(path, sha256)
            if getattr(llm, 'LOCAL', None) and getattr(llm.LOCAL, '_started', False):
                threading.Thread(target=llm.LOCAL.discover, daemon=True).start()
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)

    def _finish_job(self, plan, work):
        with self.lock:
            if self._stale():
                raise download.Stopped('cancelled')
            if plan.get('copy'):
                self._finish_copy(plan)
            elif plan['switch']:
                lib = work / 'unpack' / DATA_NAME
                if not install.is_library(lib):
                    raise install.Refused('activate', 'The new library is not complete.')
                install.stage_switch(_pending_file(self.settings_dir), lib, plan['target'], plan.get('model_file'), model_sha256=plan.get('model_sha256'))
            elif plan.get('map_new'):
                install.stage_switch(_pending_file(self.settings_dir), plan['map_new'], plan['target'], kind='map')
            if plan.get('repair') and 'damaged' in plan:   # a full check ran (Check now, or Repair's first step): keep when, and what it found
                map_ok = False if plan.get('map_damaged') else None              # a state map's files are checked against map-set.json (#88)
                bad = len(plan['damaged']) + (plan.get('model_ok') is False) + (map_ok is False)   # the model and the map count as damaged too
                result = {'at': _now(), 'damaged': bad, 'model_ok': plan.get('model_ok'), 'map_ok': map_ok,
                          'repaired': bool(bad) and not plan.get('check_only'),
                          'pack_replaced': bool(plan['damaged']) and bool(plan.get('switch'))}   # the whole pack again, the old kept aside
                self.job['result'] = result
                s = self._settings()
                s.setdefault('checks', {})[plan['target']] = result
                self._save_settings(s)
            self.job['state'] = 'done'
            for it in self.job['items']:
                it['rate_bps'], it['eta_s'] = 0, None
            self._save_job()


WINDOWS_MAX_PATH = 259      # characters in a path Windows programs can open with long paths off (the default): MAX_PATH less the end
LONGEST_INSIDE = 190        # the longest file path inside LastbookData in any pack (Medium and All, measured 12 Nov 2026; Small 166)


def path_too_deep(target, windows=None):
    """On Windows the library reads its files by ordinary paths, so <target>\\<longest file> must fit in WINDOWS_MAX_PATH."""
    windows = os.name == 'nt' if windows is None else windows
    return windows and len(str(target)) + 1 + LONGEST_INSIDE > WINDOWS_MAX_PATH


def _pack_parts_folder(work):
    return work / 'downloads' / 'pack-parts'


def _pack_download(pack, work, sources, *, keep_original=False):
    if not pack or not pack.get('parts'):
        return pack
    original = pack['files'][0]
    name = original['name']
    plan = {k: v for k, v in pack.items() if k != 'parts'}
    downloads = work / 'downloads'
    if keep_original or name in sources or (downloads / name).is_file() or (downloads / (name + '.part')).is_file():
        return plan
    return {**plan, 'files': pack['parts'], 'split_archive': original['sha256']}


def _check_pack_paths(*paths):
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        reparse = getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        hardlink = stat.S_ISREG(info.st_mode) and info.st_nlink > 1
        if reparse or hardlink or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise download.Failed('folder', 'The setup folder contains a link or special file. Choose a different library folder.')


def _map_damaged(target, what):
    """True when the library's state map does not match its map-set.json (it came from its own download, not the pack's)."""
    try:
        install.check_map(target)
        return False
    except install.Refused as e:
        install.say(f'setup: {what}:', e.message)
        return True


def _tag_sources(job):
    """Each item's source: 'found' when all its files are already on this computer (plan.sources), else 'download'."""
    plan = job['plan']
    for it in job['items']:
        if it['kind'] == 'pack' and plan.get('pack'):
            names = [f['name'] for f in plan['pack']['files']]
        elif it['kind'] == 'map' and plan.get('map'):
            names = [f['name'] for f in plan['map']['files']]
        elif it['kind'] == 'addresses' and plan.get('addresses'):
            names = [f['name'] for f in plan['addresses']['files']]
        elif it['kind'] == 'model' and plan.get('model'):
            names = [plan['model']['file']]
        else:
            names = []
        it['source'] = 'found' if names and all(n in plan['sources'] for n in names) else 'download'


def _map_need(mp, have):
    """Room a state map needs: what is still to download plus its unpacked size."""
    if not mp:
        return 0
    return sum(f['size'] for f in mp['files'] if f['name'] not in have) + (mp.get('installed_bytes') or 0)


def _margin(total):
    """Room left free beyond what an install needs: 5% of the disk, at least 5 GB and at most 20 GB (5% of an 8 TB disk would
    rule out every library)."""
    return max(RECOMMEND_MARGIN, min(int((total or 0) * 0.05), RECOMMEND_MARGIN_MAX))


def _fresh_unpack(staged, stamp, keep=True):
    """The unpack folder keeps whole files from an earlier run, so a resumed job does not unpack them again. That is only right
    for the same pack: files left by another pack (or another revision) would be taken as whole when their size matches. So the
    folder carries a note of what it was unpacked from, and is emptied when that is not what comes next. The new note is written
    only once the folder is empty: a file that cannot be removed (still open on Windows) stops setup instead of being reused.
    keep: a library's unpack, where a failed switch may have left the person's model; a map or address unpack holds only
    downloads (an address index is under derived/geo/addresses, as the person's own would be), so nothing there is set aside."""
    note = Path(staged) / 'unpacked-from.json'
    try:
        # Allow whitespace and Windows newlines while bounding a receipt read from the install drive.
        limit = max(4096, 2 * len(json.dumps(stamp, indent=1)))
        with note.open('rb') as stream:
            raw = stream.read(limit + 1)
        same = len(raw) <= limit and json.loads(raw) == stamp
    except (OSError, ValueError):
        same = False
    if not same and Path(staged).exists():
        if keep and _set_aside(staged, _kept_name(Path(staged).parent)):     # a model a failed switch left here: kept, never emptied away
            raise install.Refused('unpack', 'Lastbook could not set your files aside from the previous download. '
                                            'Close anything that may be using them, then try again.')
        try:
            note.unlink(missing_ok=True)
            shutil.rmtree(staged)
        except OSError as e:
            raise install.Refused('unpack', f'Lastbook could not clear the files of the previous download ({type(e).__name__}). '
                                            'Close anything that may be using them, then try again.')
    Path(staged).mkdir(parents=True, exist_ok=True)
    install._write_json(note, stamp)


def _tree_size(p):
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n

_separate_map = install.has_separate_map


# ------------------------------------------------------------------------------------------------ startup and the app --
def _work_for(target):
    """Setup's work folder for a library: <its name>.setup beside it (LastbookData.setup for every library setup installs)."""
    return Path(target).with_name(Path(target).name + WORK_SUFFIX)


def _job_file(settings_dir):
    """The job and the pending switch live in the app's own settings folder, never in the work folder beside a library:
    a LastbookData.setup can arrive with a folder or a drive, and what is in it is not obeyed (audit E3)."""
    return Path(settings_dir) / 'setup' / 'job.json'


def _pending_file(settings_dir):
    return Path(settings_dir) / 'setup' / 'pending.json'


def startup(settings_dir, only=None):
    """app/main.py calls this first: a staged switch is made now, before the engine opens anything. Returns the library to open
    (or None to look as usual) and a note: {target, old, model_file, error, again: a refused switch kept for the next start}."""
    settings_dir = Path(settings_dir)
    try:
        s = json.loads((settings_dir / 'setup.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, None
    work = s.get('work')
    if not work or not isinstance(work, str):
        return None, None
    if only is not None and install._real(work) != install._real(_work_for(only)):
        return None, None        # opened with --data: a switch staged for another library waits for a normal start
    pend = _pending_file(settings_dir)
    try:
        return install.apply_pending(pend, work)
    except Exception as e:        # whatever is wrong with it, the app still starts, on the library as it is
        print('setup: the pending switch was not carried out:', type(e).__name__, e, flush=True)
        try:
            pend.unlink()
        except OSError:
            pass
        return None, None


def _keep_unpacked(work, note):
    """A failed switch leaves the staged library, which may hold the person's model. Set it aside as kept-<time>, so the next
    job unpacks fresh and _fresh_unpack never empties a folder with the model in it."""
    if work.is_symlink():
        return
    staged = work / 'unpack'
    kept = _kept_name(work)
    try:
        os.replace(staged, kept)
    except OSError:     # something in it is still open: set aside only the person's files, and drop the rest so none is reused
        stuck = _set_aside(staged, kept)     # what could not move is the last copy: it stays where it is, and the message says so
        blocked = _drop_except(staged, stuck)       # #86's note goes too: a file still open stops the next job instead of being reused
        if stuck:
            note['error'] = str(note['error']) + ' Your files could not be moved and are still in ' + ', '.join(map(str, stuck)) + '.'
        if blocked:
            note['error'] = str(note['error']) + ' These files are still open and were left in place: ' + ', '.join(map(str, blocked)) + '.'
        if not any(p.exists() for p in (kept / DATA_NAME / part for part in _PERSONS_FILES)):
            shutil.rmtree(kept, ignore_errors=True)
            return
    else:
        note['error'] = str(note['error']).replace(str(staged), str(kept))
    note['error'] = str(note['error']) + f' What was unpacked is kept in {kept}.'


_PERSONS_FILES = (Path('models'), Path('derived', 'geo', 'addresses'))     # what install.carry_over moves into a new library


def _kept_name(work):
    return Path(work) / time.strftime('kept-%Y%m%d-%H%M%S')


def _set_aside(staged, kept):
    """Moves the person's files from the library in `staged` into `kept`. Returns the folders that could not move."""
    stuck = []
    for part in _PERSONS_FILES:
        src = Path(staged) / DATA_NAME / part
        if src.is_dir():
            try:
                (kept / DATA_NAME / part).parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, kept / DATA_NAME / part)
            except OSError:
                stuck.append(src)
    return stuck


def _drop_except(p, keep, blocked=None):
    """Deletes `p`, except the folders in `keep` (and the folders that hold them). A file that cannot be removed (still open on
    Windows) is left where it is; returns those paths, so the message can name them."""
    blocked = [] if blocked is None else blocked
    if p in keep:
        return blocked
    if p.is_dir() and not p.is_symlink() and any(p in k.parents for k in keep):
        for c in p.iterdir():
            _drop_except(c, keep, blocked)
    elif p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, onerror=lambda f, path, e: blocked.append(Path(path)))
    else:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            blocked.append(p)
    return blocked


def _clear_work(work, s):
    """A setup that worked: what jobs made goes. A folder a failed switch set aside stays: it may hold the person's model."""
    if work.is_symlink():     # never follow it: it may point at a library (rmtree refused it before, too)
        s.pop('work', None)
        return
    for p in (work.iterdir() if work.is_dir() else ()):
        if not p.name.startswith('kept-'):
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    if not any(work.glob('kept-*')):
        shutil.rmtree(work, ignore_errors=True)
        s.pop('work', None)


def opened(settings_dir, data, note):
    """The library opened. Record the old one for deleting later, mark the model checked, and clear the work folder."""
    settings_dir = Path(settings_dir)
    try:
        s = json.loads((settings_dir / 'setup.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        s = {}
    work = Path(s['work']) if s.get('work') else None
    if note and note.get('model_file') and note.get('model_sha256') and os.path.isfile(note['model_file']):
        try:
            import llm_local      # imported here, after LASTBOOK_DATA names the library: its state file lives there
            llm_local.mark_checked(note['model_file'], note['model_sha256'])
        except Exception as e:
            print('setup: mark_checked:', type(e).__name__, e, flush=True)
    if note and note.get('old') and note['old'] not in s.get('old', []):
        s.setdefault('old', []).append(note['old'])
    if work and note and note.get('again'):      # a switch kept for the next start keeps its pending file and its unpack;
        try:                                     # its finished job goes, so setup manages the library in use meanwhile
            _job_file(settings_dir).unlink()
        except OSError:
            pass
    elif work:
        install.confirm(_pending_file(settings_dir))
        try:
            job = json.loads(_job_file(settings_dir).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            job = None
        if note or (isinstance(job, dict) and job.get('state') == 'done' and not job.get('check')):   # a check installed nothing
            if note and note.get('error'):
                _keep_unpacked(work, note)
            else:
                _clear_work(work, s)
            try:
                _job_file(settings_dir).unlink()
            except OSError:
                pass
    s['old'] = [o for o in s.get('old', []) if Path(o).exists()]
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        install._write_json(settings_dir / 'setup.json', s)
    except OSError:
        pass


def saved_model_server(settings_dir):
    """(base, model) of the model server chosen in Setup, or None: nothing chosen, or a setup.json that no longer holds a loopback address."""
    try:
        got = json.loads((Path(settings_dir) / 'setup.json').read_text(encoding='utf-8')).get('model_server')
        base, model = model_server.loopback_base(got['base']), got['model']
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None
    return (base, model) if isinstance(model, str) and 0 < len(model) <= model_server.MAX_ID else None


def follow_model_server(use):
    """The open library's assistant follows the server chosen in Setup: use(base, model) now with the saved one, and on each
    new choice. A server named when starting Lastbook (SURVIVAL_MODEL_BASE) wins over the saved one until the person chooses again."""
    s = SETUP or _default()
    s.on_model_server = use
    chosen = saved_model_server(s.settings_dir)
    if chosen and 'SURVIVAL_MODEL_BASE' not in os.environ:
        use(*chosen)


SETUP = None


def configure(**kw):
    global SETUP
    SETUP = Setup(**kw)
    return SETUP


def _default():
    global SETUP
    if SETUP is None:
        from engine import paths
        if sys.platform == 'win32':
            settings = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Lastbook'
        elif sys.platform.startswith('linux'):
            settings = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'Lastbook'
        else:
            settings = Path.home() / 'Library' / 'Application Support' / 'Lastbook'
        SETUP = Setup('manage', settings, paths.DATA, current=paths.DATA)
    return SETUP


ROUTES = {
    ('GET', 'state'): lambda s, b: s.state(),
    ('GET', 'job'): lambda s, b: s.public_job(),
    ('POST', 'start'): lambda s, b: s.start(b.get('pack'), bool(b.get('ai')), b.get('folder'), repair=bool(b.get('repair')), map_id=b.get('map') or None, address_id=b.get('address_id') or None, copy_to=b.get('copy_to') or None, check=b.get('check') is True),
    ('POST', 'pause'): lambda s, b: s.pause(),
    ('POST', 'resume'): lambda s, b: s.resume(),
    ('POST', 'cancel'): lambda s, b: s.cancel(),
    ('POST', 'retry'): lambda s, b: s.retry(),
    ('POST', 'use'): lambda s, b: s.use(b.get('path'), library_only=b.get('library') is True, folder=b.get('folder')),
    ('POST', 'finish'): lambda s, b: s.finish(),
    ('POST', 'pick'): lambda s, b: s.pick(files=b.get('files') is True),
    ('POST', 'delete-old'): lambda s, b: s.delete_old(b.get('path')),
    ('POST', 'remove-ai'): lambda s, b: s.remove_ai(),
    ('POST', 'server-models'): lambda s, b: s.server_models(b.get('address')),
    ('POST', 'use-server'): lambda s, b: s.use_server(b.get('address'), b.get('model')),
    ('POST', 'stop-server'): lambda s, b: s.stop_server(),
    ('GET', 'updates'): lambda s, b: _updates(),
    ('POST', 'updates'): lambda s, b: _set_update_notice(b.get('notify')),
}


def _updates():
    """The desktop app exposes this preference in Setup and Settings."""
    from engine import update
    n = update.NOTICES
    return dict(n.notice(), supported=True) if n else {'supported': False, 'notify': False}


def _set_update_notice(notify):
    from engine import update
    if not isinstance(notify, bool):
        return {'error': 'notify is true or false.', 'code': 'bad_request'}
    if update.NOTICES is None:
        return {'error': 'App updates are set in the desktop app.', 'code': 'unsupported'}
    return dict(update.NOTICES.set_notify(notify), supported=True)


def _send_json(h, obj, status=200):
    data = json.dumps(obj, separators=(',', ':')).encode()
    h.send_response(status)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Content-Length', str(len(data)))
    h.send_header('Cache-Control', 'no-store')
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(data)


def handle(h, method, path):
    """Answers one /api/setup/ request on a BaseHTTPRequestHandler h."""
    if h.client_address[0] not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return _send_json(h, {'error': 'Setup is only open on this computer.'}, 403)
    name = path[len('/api/setup/'):].strip('/')
    fn = ROUTES.get(('GET' if method == 'HEAD' else method, name))
    if not fn:
        return _send_json(h, {'error': 'not found'}, 404)
    body = {}
    if method == 'POST':
        try:
            n = int(h.headers.get('Content-Length') or 0)
            if n < 0 or n > MAX_BODY:
                return _send_json(h, {'error': 'request too large'}, 413)
            body = json.loads(h.rfile.read(n) or b'{}') if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _send_json(h, {'error': 'bad request'}, 400)
    try:
        out = fn(SETUP or _default(), body)
    except Exception as e:
        traceback.print_exc()
        return _send_json(h, {'error': f'Setup stopped on an internal error ({type(e).__name__}).'}, 500)
    status = 200
    if isinstance(out, dict) and out.get('error') and 'code' in out:
        status = 409 if out['code'] in ('busy', 'not_done') else 400 if out['code'] != 'no_space' else 507
    return _send_json(h, out, status)


# ------------------------------------------------------------------------------------------------ setup mode's Handler --
def web_dir():
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent)) / 'web'
    return Path(__file__).resolve().parents[2] / 'web'


TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml',
         '.png': 'image/png', '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.woff2': 'font/woff2'}


class SetupHandler(BaseHTTPRequestHandler):
    """All there is in setup mode: the setup page, the app's web files, and /api/setup/*."""
    server_version = 'Lastbook'
    sys_version = ''
    NO_CODE_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; worker-src 'none'; object-src 'none'; manifest-src 'none'; "
                   "base-uri 'none'; form-action 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:")
    SAVED_PAGE_CSP = 'sandbox; ' + NO_CODE_CSP

    def log_message(self, *a):
        pass

    def _path(self):
        import urllib.parse
        return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

    def _static(self, p):
        try:
            data = p.read_bytes()
        except OSError:
            return _send_json(self, {'error': 'not found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', TYPES.get(p.suffix, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_GET(self):
        path = self._path()
        if '\0' in path or '/..' in path or '\\' in path:
            return _send_json(self, {'error': 'not found'}, 404)
        web = web_dir()
        if path in ('/', '/index.html', '/ui/setup.html'):
            return self._static(web / 'setup.html')
        if path.startswith('/ui/'):
            p = (web / path[4:]).resolve()
            if web.resolve() in p.parents and p.is_file():
                return self._static(p)
            return _send_json(self, {'error': 'not found'}, 404)
        if path.startswith('/api/setup/'):
            return handle(self, self.command, path)
        return _send_json(self, {'error': 'not found'}, 404)

    do_HEAD = do_GET

    def do_POST(self):
        path = self._path()
        if path.startswith('/api/setup/'):
            return handle(self, 'POST', path)
        return _send_json(self, {'error': 'not found'}, 404)


class SetupServer:
    """What app/bridge.py needs of an engine module: its Handler."""
    Handler = SetupHandler
