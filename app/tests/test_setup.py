#!/usr/bin/env python3
"""First-run setup, engine side (engine/setup/), against a local HTTPS test server. No real internet, never the real home folder.

  derived/app-build/mac-venv/bin/python -B app/tests/test_setup.py [-v] [Class.test_name]
  LASTBOOK_FULL_DISK=1 ...   also runs the full-disk case on a small disk image (hdiutil, macOS only)

The test server lives only in this file: it serves a small fake pack and model over HTTPS on 127.0.0.1 with a certificate made
for the run, and can cut the connection mid-file, send wrong bytes, change the file's ETag, ignore Range, or redirect anywhere.
The product's allowlist is not touched: tests pass their own `allowed` and TLS context to the downloader. Never packaged.
"""
import errno, hashlib, http.server, importlib.util, io, json, os, re, shutil, signal, socket, socketserver, ssl, struct, subprocess, sys, threading, time, unittest, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT)]
from engine.setup import api, download, hardware, install, manifest  # noqa: E402
from app.tests import multipart_fixture, shorttemp  # noqa: E402

ALLOWED = {'127.0.0.1'}     # an address, not 'localhost': Windows tries ::1 first, which the test server does not listen on
MiB = 1024 * 1024


# ------------------------------------------------------------------------------------------------------------ test data --
def make_pack(path, files=None, extra=None):
    """A zip shaped like a real pack: everything under LastbookData/, stored."""
    files = files or {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small"}',
                      'library-data/big.bin': os.urandom(3 * MiB), 'derived/geo/map.sqlite': os.urandom(MiB)}
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('LastbookData/', b'')
        for name, data in files.items():
            z.writestr('LastbookData/' + name, data)
        for info, data in (extra or []):
            z.writestr(info, data)
    return sum(len(v) for v in files.values()) + sum(len(d) for _, d in (extra or []))


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Files:
    """What the test server serves, and how it misbehaves. name -> bytes."""

    def __init__(self):
        self.data, self.etag, self.log = {}, {}, []
        self.cut_after = {}       # name -> bytes after which the next answer is cut (once per entry in the list)
        self.corrupt = {}         # name -> how many more answers carry a flipped byte
        self.ignore_range = set()
        self.ignore_once = {}     # name -> how many more Range requests get a 200 with the whole file
        self.redirect = {}        # name -> Location
        self.answers = {}         # name -> answers to give in turn: (status, Retry-After or None), 'whole' (a 200 with the whole
                                  # file, even to a Range, that leaves cut_after for the next) or None (served as usual)
        self.pace = 0             # seconds to sleep per 64 KiB
        self.hold = {}            # name -> Hold: the next answer that covers its byte stops there until the test lets it go
        self.lock = threading.Lock()


class Hold:
    """Holds one answer open at byte `at`, so a test can pause or cancel mid-transfer without guessing at timings.
    .reached is set once the bytes before `at` are sent; the answer then waits for .release (or 30 s) before sending the rest."""

    def __init__(self, at):
        self.at, self.reached, self.release = at, threading.Event(), threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        F = self.server.files
        name = self.path.split('?')[0].lstrip('/')
        with F.lock:
            F.log.append((name, self.headers.get('Range'), self.headers.get('If-Range')))
        if name in F.redirect:
            self.send_response(302)
            self.send_header('Location', F.redirect[name])
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        with F.lock:
            answer = F.answers[name].pop(0) if F.answers.get(name) else None
        if isinstance(answer, tuple):
            self.send_response(answer[0])
            if answer[1] is not None:
                self.send_header('Retry-After', str(answer[1]))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if name not in F.data:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = F.data[name]
        with F.lock:
            if F.corrupt.get(name):
                F.corrupt[name] -= 1
                body = bytes([body[0] ^ 0xff]) + body[1:]
        start, rng = 0, self.headers.get('Range')
        once = answer == 'whole'
        with F.lock:
            if rng and not once and F.ignore_once.get(name):
                F.ignore_once[name] -= 1
                once = True
        end = len(body)                                         # past the last byte sent
        if rng and name not in F.ignore_range and not once:     # like R2: If-Range is ignored
            a, _, b = rng.split('=')[1].partition('-')
            start = int(a)
            if b:
                end = min(int(b) + 1, len(body))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end - 1}/{len(body)}')
        else:
            self.send_response(200)
        self.send_header('Content-Length', str(end - start))
        self.send_header('ETag', F.etag.get(name, '"e1"'))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        cut = hold = None
        with F.lock:
            cuts = F.cut_after.get(name)
            if cuts and answer != 'whole':
                cut = cuts.pop(start, None) if isinstance(cuts, dict) else cuts.pop(0)
            if name in F.hold and start <= F.hold[name].at < end:
                hold = F.hold.pop(name)
        sent = start
        try:
            while sent < end:
                if hold is not None and hold.at < sent + 64 * 1024:
                    self.wfile.write(body[sent:hold.at])
                    self.wfile.flush()
                    sent = hold.at
                    hold.reached.set()
                    hold.release.wait(30)
                    hold = None
                    continue
                n = min(64 * 1024, end - sent)
                if cut is not None and sent + n > cut:
                    self.wfile.write(body[sent:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                self.wfile.write(body[sent:sent + n])
                sent += n
                if F.pace:
                    time.sleep(F.pace)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class TLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A client that pauses, cancels or is killed drops its connection, which is what these tests ask for. On Windows the
        server's next read then raises ConnectionResetError, and the default handler prints a traceback into a passing run.
        Anything else is still printed."""
        if not isinstance(sys.exception(), ConnectionError):
            super().handle_error(request, client_address)


def certificate(folder):
    key, crt = folder / 'key.pem', folder / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1', '-keyout', str(key), '-out', str(crt)],
                   check=True, capture_output=True)
    return key, crt


def start_server(folder, files):
    key, crt = certificate(folder)
    srv = TLSServer(('127.0.0.1', 0), Handler)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(crt, key)
    srv.socket = sctx.wrap_socket(srv.socket, server_side=True)
    srv.files = files
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cctx = ssl.create_default_context(cafile=str(crt))
    return srv, cctx, f'https://127.0.0.1:{srv.server_address[1]}'


class ResetServer:
    """A host this network cannot reach: every connection is accepted and at once reset, before any answer, as a network that
    blocks a host does (#254). .url(name) is an address on it the tests allow."""

    def __init__(self):
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.count = 0
        threading.Thread(target=self.serve, daemon=True).start()

    def url(self, name):
        return f'https://127.0.0.1:{self.sock.getsockname()[1]}/{name}'

    def serve(self):
        linger = struct.pack('HH' if sys.platform == 'win32' else 'ii', 1, 0)    # close with a reset, not a goodbye
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.count += 1
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            c.close()

    def close(self):
        self.sock.close()


class Control:
    def __init__(self):
        self.ev = threading.Event()

    def check(self):
        pass


# ------------------------------------------------------------------------------------------------------------- base --
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_class = shorttemp.mkdtemp('lastbook-setup-srv-')
        cls.files = Files()
        cls.srv, cls.ctx, cls.base = start_server(cls.tmp_class, cls.files)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()       # shutdown stops serving; close the fixture listener as well
        shutil.rmtree(cls.tmp_class, ignore_errors=True)

    def setUp(self):
        F = self.files
        F.data.clear(); F.etag.clear(); F.log.clear(); F.cut_after.clear(); F.corrupt.clear(); F.ignore_range.clear(); F.ignore_once.clear(); F.redirect.clear(); F.answers.clear()
        F.hold.clear()
        F.pace = 0
        self.tmp = shorttemp.mkdtemp('lastbook-setup-')
        self.home = self.tmp / 'home'
        for d in ('Downloads', 'Documents', 'Desktop'):
            (self.home / d).mkdir(parents=True)
        self.settings = self.tmp / 'settings'
        self.parent = self.tmp / 'place'
        zpath = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(zpath)
        self.zip = zpath.read_bytes()
        self.model = os.urandom(2 * MiB + 123)
        F.data['Lastbook-Small.zip'] = self.zip
        F.data['model.gguf'] = self.model
        self.marked = []
        self.llm = mock.Mock()
        self.llm.mark_checked = lambda p, sha256: self.marked.append((str(p), sha256))
        self.llm.LOCAL = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manifest(self, zip_sha=None, model_sha=None, more_packs=(), model_url=None):
        m = {'version': 1, 'published': '2026-11-12',
             'packs': [{'id': 'small', 'title': 'Small', 'summary': 's', 'records': 3, 'installed_bytes': self.installed, 'available': True,
                        'files': [{'name': 'Lastbook-Small.zip', 'url': f'{self.base}/Lastbook-Small.zip', 'size': len(self.zip),
                                   'sha256': zip_sha or sha(self.zip), 'kind': 'zip'}]},
                       {'id': 'all', 'title': 'All', 'available': False, 'files': []}],
             'models': [{'id': 'gemma', 'title': 'Gemma', 'file': 'model.gguf', 'url': model_url or f'{self.base}/model.gguf', 'size': len(self.model),
                         'sha256': model_sha or sha(self.model), 'memory_bytes': 19_027_384_256, 'available': True}]}
        m['packs'][1:1] = more_packs
        v = manifest.validate(m, ALLOWED)
        self.assertIsNotNone(v)
        return manifest.Manifest(bundled=v, remote=None, allowed=ALLOWED, context=self.ctx)

    def setup(self, mode='first-run', current=None, **kw):
        kw.setdefault('manifest', self.manifest())
        return api.Setup(mode, self.settings, self.parent, current=current, allowed=ALLOWED, context=self.ctx, home=self.home,
                         llm_local=self.llm, **kw)

    def wait(self, s, states=('done', 'failed', 'paused'), timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = s.public_job()
            if j and j['state'] in states:
                if s.thread:
                    s.thread.join(10)
                return s.public_job()
            time.sleep(0.02)
        self.fail(f'job never reached {states}: {s.public_job()}')


class fast_backoff:
    """Waits between tries shortened to 0.05 s, for broken connections and for a busy server alike."""

    def __enter__(self):
        self.p = [mock.patch.object(download, 'BACKOFF', (0.05,) * 6), mock.patch.object(download, 'BUSY_BACKOFF', (0.05,) * 6)]
        for x in self.p:
            x.start()

    def __exit__(self, *a):
        for x in self.p:
            x.stop()


# ----------------------------------------------------------------------------------------------------------- tests --
class ReleaseManifest(unittest.TestCase):
    def test_rejects_invalid_generated_part_before_replacing_manifest(self):
        from app.tests.multipart_fixture import write_fixture
        from tempfile import TemporaryDirectory

        spec = importlib.util.spec_from_file_location('release_manifest', ROOT / 'packaging/release_manifest.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'fixture'
            write_fixture(folder)
            output = folder / 'single.json'
            original = output.read_bytes()
            parts = folder / 'generated'
            args = ['release_manifest', '--packs', str(output), '--out', str(output), '--part-bytes', '128',
                    '--split-pack', str(folder / 'Lastbook-Small.zip'), '--parts-dir', str(parts)]
            # The input ZIP is pinned to the correct host. Only the generated part URLs are invalid.
            with mock.patch.object(builder, 'BASE', 'https://example.invalid'), mock.patch.object(sys, 'argv', args):
                with self.assertRaisesRegex(SystemExit, 'parts'):
                    builder.main()
            self.assertEqual(output.read_bytes(), original)
            self.assertFalse(parts.exists())


class Allowlist(unittest.TestCase):
    def test_product_hosts(self):
        ok = ['https://downloads.lastbook.org/1.0.0/Lastbook-Small.zip',
              'https://huggingface.co/unsloth/x/resolve/abc/f.gguf?download=true', 'https://cas-bridge.xethub.hf.co/xet-bridge-us/abc?X-Amz=1',
              'https://us.gcp.cdn.hf.co/x']
        bad = ['http://downloads.lastbook.org/x', 'https://downloads.lastbook.org:8443/x', 'https://evil.com/x', 'https://hf.co.evil.com/x',
               'https://user:pw@downloads.lastbook.org/x', 'https://lastbook.org/x', 'https://other.r2.dev/x', 'https://pub-28c3cc06eb774c5cac057e6c9343efbd.r2.dev/x.zip', 'ftp://huggingface.co/x',
               'https://.hf.co/x', 'https://localhost/x', 'https://127.0.0.1/x', 'file:///etc/passwd', 'https://downloads.lastbook.org.evil.com/x']
        for u in ok:
            self.assertTrue(download.url_allowed(u), u)
        for u in bad:
            self.assertFalse(download.url_allowed(u), u)

    def test_no_proxy(self):
        """http.client takes no proxy from the environment: a request with HTTPS_PROXY set still goes to the host named."""
        import inspect
        src = inspect.getsource(download)
        self.assertNotIn('urllib.request', src)
        self.assertNotIn('ProxyHandler', src)
        self.assertNotIn('set_tunnel', src)

    def test_only_module_with_connections(self):
        """In engine/setup/, only download.py touches http.client, socket connections or urllib.request."""
        for p in (ROOT / 'engine' / 'setup').glob('*.py'):
            if p.name == 'download.py':
                continue
            t = p.read_text(encoding='utf-8')
            for word in ('http.client', 'urllib.request', 'socket.create_connection', 'HTTPSConnection', 'import socket'):
                self.assertNotIn(word, t, f'{p.name}: {word}')

    def test_bundled_manifest_urls(self):
        p = ROOT / 'derived' / 'release' / 'setup' / 'manifest-v1.json'
        if not p.is_file():
            self.skipTest('no bundled manifest built yet (packaging/release_manifest.py)')
        m = manifest.validate(json.loads(p.read_text()))
        self.assertIsNotNone(m)
        self.assertTrue(m['models'] and m['packs'])

    def test_fetched_manifest_keeps_bundled_entries(self):
        bundled = manifest.validate({'version': 1, 'published': '2026-11-12', 'packs': [
            {'id': 'small', 'available': True, 'installed_bytes': 5, 'files': [{'name': 'a.zip', 'url': 'https://downloads.lastbook.org/a.zip', 'size': 1, 'sha256': '0' * 64}]}],
            'models': [{'id': 'gemma', 'file': 'm.gguf', 'url': 'https://huggingface.co/m', 'size': 1, 'sha256': '1' * 64, 'memory_bytes': 1}]})
        fetched = manifest.validate({'version': 1, 'published': '2026-11-13', 'packs': [
            {'id': 'medium', 'available': True, 'installed_bytes': 9, 'files': [{'name': 'b.zip', 'url': 'https://downloads.lastbook.org/b.zip', 'size': 2, 'sha256': '2' * 64}]},
            {'id': 'small', 'title': 'Small v2', 'available': True, 'installed_bytes': 6, 'files': [{'name': 'a.zip', 'url': 'https://downloads.lastbook.org/a.zip', 'size': 3, 'sha256': '3' * 64}]}],
            'models': []})
        m = manifest.Manifest(bundled=bundled, remote='https://downloads.lastbook.org/manifest-v1.json')
        with mock.patch.object(download, 'fetch_small', lambda *a, **k: json.dumps(fetched).encode()):
            m._fetch()
        self.assertEqual(m.source, 'fetched')
        self.assertEqual([p['id'] for p in m.current['packs']], ['medium', 'small'])
        self.assertEqual(m.pack('small')['title'], 'small')             # in both: the bundled entry is pinned (audit E1)
        self.assertEqual(m.pack('small')['files'][0]['sha256'], '0' * 64)
        self.assertEqual(m.model()['id'], 'gemma')                        # fetched had none: the bundled one stays

    def test_fetched_manifest_rules(self):
        m = {'version': 1, 'packs': [{'id': 'small', 'available': True, 'installed_bytes': 5,
                                      'files': [{'name': 'a.zip', 'url': 'https://evil.com/a.zip', 'size': 1, 'sha256': '0' * 64}]}], 'models': []}
        self.assertIsNone(manifest.validate(m))
        m['packs'][0]['files'][0]['url'] = 'https://downloads.lastbook.org/a.zip'
        self.assertIsNotNone(manifest.validate(m))
        self.assertIsNone(manifest.validate({**m, 'version': 2}))
        m['packs'][0]['files'][0]['name'] = '../a.zip'
        self.assertIsNone(manifest.validate(m))


class Downloads(Base):
    def dl(self, name, sha256=None, size=None, control=None):
        data = self.files.data[name]
        return download.Download(f'{self.base}/{name}', self.tmp / 'dl' / name, size or len(data), sha256 or sha(data),
                                 control or download.Control(), allowed=ALLOWED, context=self.ctx)

    def test_plain(self):
        p = self.dl('model.gguf').run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertFalse(Path(p + '.part').exists())

    def test_cut_mid_file_resumes_with_range(self):
        self.files.cut_after['model.gguf'] = [700_000, 1_500_000]
        with fast_backoff():
            p = self.dl('model.gguf').run()
        self.assertEqual(sha(Path(p).read_bytes()), sha(self.model))
        ranges = [r for n, r, _ in self.files.log if n == 'model.gguf']
        self.assertEqual(ranges[0], None)
        self.assertTrue(all(r and r.startswith('bytes=') for r in ranges[1:]), ranges)
        self.assertEqual(len(ranges), 3)

    def test_resume_says_downloading_once_the_part_is_read_again(self):
        """A resumed download reads its part again as 'verifying', then says 'downloading' before it asks the server, so a wait for
        a busy server or a dropped connection is not shown as checking."""
        data, seen = self.files.data['model.gguf'], []
        d = download.Download(f'{self.base}/model.gguf', self.tmp / 'dl' / 'model.gguf', len(data), sha(data), download.Control(),
                              lambda done, total, phase: seen.append((phase, done)), allowed=ALLOWED, context=self.ctx)
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(data[:600_000])
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"e1"', 'bytes_done': 600_000}))
        self.files.answers['model.gguf'] = [(503, None)]
        with fast_backoff():
            self.assertEqual(Path(d.run()).read_bytes(), data)
        last = max(i for i, (phase, _) in enumerate(seen) if phase == 'verifying')
        self.assertEqual(seen[last], ('verifying', 600_000))
        self.assertEqual(seen[last + 1], ('downloading', 600_000))

    def test_busy_server_is_waited_out(self):
        """downloads.lastbook.org cut a 388 GB download, then answered its resume with 503 for minutes: more 503s in a row than
        NETWORK_TRIES. A busy server is waited on longer, and the download finishes from where it was cut."""
        self.files.cut_after['model.gguf'] = [700_000]
        self.files.answers['model.gguf'] = [None] + [(503, None)] * (download.NETWORK_TRIES + 2) + [(429, None)]   # None: served (and cut)
        with fast_backoff():
            p = self.dl('model.gguf').run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        ranges = [r for n, r, _ in self.files.log if n == 'model.gguf']
        self.assertEqual(ranges[0], None)
        self.assertEqual(set(ranges[1:]), {'bytes=700000-'})                          # every retry resumed, nothing started over

    def test_busy_server_gives_up_as_server(self):
        """A server busy for good fails with 'server', not 'network': the person's internet is fine. The waits add up to
        BUSY_LIMIT, however long a Retry-After the server asks for."""
        for retry_after in (None, 600):
            with self.subTest(retry_after=retry_after):
                self.files.log.clear()
                self.files.answers['model.gguf'] = [(503, retry_after)] * 30
                waits, d = [], self.dl('model.gguf')
                d.control.sleep = waits.append
                with self.assertRaises(download.Failed) as e:
                    d.run()
                self.assertEqual(e.exception.code, 'server')
                self.assertEqual(sum(waits), download.BUSY_LIMIT)
                self.assertEqual(len(self.files.log), len(waits) + 1)      # one last ask after the last wait

    def test_progress_renews_the_busy_budget(self):
        """Bytes that arrive before a connection breaks are progress, so the busy waits before them no longer count against
        BUSY_LIMIT. Before, only the broken-connection count was reset there, and this ran out of budget on the fourth ask."""
        self.files.cut_after['model.gguf'] = [700_000]
        self.files.answers['model.gguf'] = [(503, None), None, (503, None), (503, None)]
        waits, d = [], self.dl('model.gguf')
        d.control.sleep = waits.append
        with mock.patch.object(download, 'BUSY_BACKOFF', (10,) * 6), mock.patch.object(download, 'BUSY_LIMIT', 20), \
                mock.patch.object(download, 'BACKOFF', (0,) * 6):
            p = d.run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertEqual(waits, [10, 0, 10, 10])
        self.assertEqual(self.files.log[-1][1], 'bytes=700000-')

    def test_progress_renews_the_stray_200_allowance(self):
        """A 200 to a resume is asked about once more before starting over. After progress that allowance is there again, so a
        second passing 200 hours into a large download does not throw away what is downloaded."""
        self.files.cut_after['model.gguf'] = [500_000, 1_000_000]
        self.files.answers['model.gguf'] = [None, 'whole', None, 'whole']
        with fast_backoff():
            p = self.dl('model.gguf').run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertEqual([r for _, r, _ in self.files.log], [None, 'bytes=500000-', 'bytes=500000-', 'bytes=1000000-', 'bytes=1000000-'])

    def test_pause_during_busy_wait_then_resume(self):
        """Pause wakes a wait on a busy server at once, the bytes already downloaded are kept, and the next run resumes from them."""
        self.files.cut_after['model.gguf'] = [700_000]
        self.files.answers['model.gguf'] = [None, (503, 300)]
        control, waits = download.Control(), []
        d = download.Download(f'{self.base}/model.gguf', self.tmp / 'dl' / 'model.gguf', len(self.model), sha(self.model), control,
                              allowed=ALLOWED, context=self.ctx, waiting=waits.append)
        threading.Timer(0.5, control.stop, ['paused']).start()
        t0 = time.time()
        with fast_backoff(), self.assertRaises(download.Stopped):
            d.run()
        self.assertLess(time.time() - t0, 5)                                   # not the 300 s the server asked for
        self.assertEqual(waits, [300])
        self.assertEqual(os.path.getsize(d.part), 700_000)
        p = self.dl('model.gguf').run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertEqual(self.files.log[-1][1], 'bytes=700000-')

    def test_retry_after_is_honoured(self):
        waits = []
        self.files.answers['model.gguf'] = [(503, 7), (503, 'Wed, 21 Oct 2015 07:28:00 GMT'), (503, 99999)]
        d = self.dl('model.gguf')
        d.control.sleep = waits.append
        with fast_backoff():
            d.run()
        self.assertEqual(waits, [7, 0.05, download.MAX_RETRY_AFTER])

    def test_etag_changed_starts_over(self):
        """R2 answers 206 whatever If-Range says, so the ETag is compared here."""
        d = self.dl('model.gguf')
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(b'x' * 600_000)          # bytes of an older copy of the file
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"old"', 'bytes_done': 600_000}))
        self.files.etag['model.gguf'] = '"new"'
        d.run()
        self.assertEqual(Path(d.dest).read_bytes(), self.model)
        self.assertEqual([r for _, r, _ in self.files.log], ['bytes=600000-', 'bytes=0-599999', None])   # tried to resume, compared what was here, then started over

    def stale(self, d, data, n, etag='"old"'):
        """Leaves the first n bytes of `data` as d's partial file, saved under an ETag the server no longer gives."""
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(data[:n])
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': etag, 'bytes_done': n}))

    def test_etag_changed_same_bytes_keeps_the_part(self):
        """The server's copy got a new ETag (uploaded again, say) but holds the same bytes: what is here is kept (#208)."""
        d = self.dl('model.gguf')
        self.stale(d, self.model, 600_000)
        self.files.etag['model.gguf'] = '"new"'
        seen = []
        d.progress = lambda done, total, phase: phase == 'downloading' and seen.append(done)
        d.run()
        self.assertEqual(Path(d.dest).read_bytes(), self.model)
        log = [r for _, r, _ in self.files.log]
        self.assertEqual(log[0], 'bytes=600000-')
        self.assertNotIn(None, log)                                   # never asked for the whole file
        self.assertTrue(all(int(r[6:].split('-')[1]) < 600_000 for r in log[1:]), log)   # the rest: samples of what was here
        self.assertEqual(min(seen), 600_000)                          # and the count never went back

    def test_etag_changed_sample_differs_starts_over(self):
        """One byte of what is here is not the server's: that is another file, and it starts over as before."""
        d = self.dl('model.gguf')
        self.stale(d, self.model[:599_999] + bytes([self.model[599_999] ^ 1]), 600_000)
        self.files.etag['model.gguf'] = '"new"'
        d.run()
        self.assertEqual(Path(d.dest).read_bytes(), self.model)
        self.assertEqual([r for _, r, _ in self.files.log][-1], None)

    def test_etag_changed_between_the_samples_is_left_to_the_checksum(self):
        """The comparison looks at stretches, not at every byte. A difference between them is found by the SHA-256 at the end, as
        for any download that came out wrong."""
        d = self.dl('model.gguf')
        self.stale(d, self.model[:150_000] + bytes([self.model[150_000] ^ 1]) + self.model[150_001:], 600_000)
        self.files.etag['model.gguf'] = '"new"'
        with mock.patch.object(download, 'SAMPLE', 1000), self.assertRaises(download.BadChecksum):
            d.run()
        self.assertEqual([r for _, r, _ in self.files.log], ['bytes=600000-', 'bytes=0-999', 'bytes=299500-300499', 'bytes=599000-599999'])
        self.assertFalse(Path(d.part).exists() or Path(d.dest).exists())

    def test_etag_changed_and_the_check_breaks_keeps_the_part(self):
        """A busy answer and a cut connection during the comparison are waited out like any other: nothing is thrown away."""
        d = self.dl('model.gguf')
        self.stale(d, self.model, 600_000)
        self.files.etag['model.gguf'] = '"new"'
        self.files.answers['model.gguf'] = [None, (503, None)]        # the resume is answered, the first sample is not
        with fast_backoff():
            d.run()
        self.assertEqual(Path(d.dest).read_bytes(), self.model)
        self.assertNotIn(None, [r for _, r, _ in self.files.log])

    def test_etag_changed_and_the_check_gets_the_whole_file_keeps_the_part(self):
        """A 200 to a sample tells nothing about what is here. It gives up as a network failure with the partial file kept."""
        d = self.dl('model.gguf')
        self.stale(d, self.model, 600_000)
        self.files.etag['model.gguf'] = '"new"'
        self.files.answers['model.gguf'] = [None, 'whole'] * download.NETWORK_TRIES     # each resume is answered, each sample is not
        with fast_backoff(), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual(e.exception.code, 'network')
        self.assertEqual(os.path.getsize(d.part), 600_000)
        self.assertEqual(json.loads(Path(d.meta).read_text())['etag'], '"old"')   # so the next try checks again

    def test_server_ignores_range(self):
        """A 200 with the whole file to a resume (seen on downloads.lastbook.org for minutes after an upload): after one more ask,
        the .part is emptied, the hash and the progress start from 0, and nothing is appended."""
        self.files.cut_after['model.gguf'] = [500_000]
        self.files.ignore_range.add('model.gguf')
        seen, sizes = [], []
        d = self.dl('model.gguf')
        d.progress = lambda done, total, phase: (seen.append(done), sizes.append(os.path.getsize(d.part)))
        real = download.Download._fetch

        def watched(self_, start, etag, h, ctx):
            sizes.append(('fetch from', start, os.path.getsize(self_.part)))
            return real(self_, start, etag, h, ctx)
        with fast_backoff(), mock.patch.object(download.Download, '_fetch', watched):
            p = d.run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertEqual([r for _, r, _ in self.files.log], [None, 'bytes=500000-', 'bytes=500000-', None])   # asked again once, then from 0
        fetches = [x for x in sizes if isinstance(x, tuple)]
        self.assertEqual(fetches[-1], ('fetch from', 0, 0))              # the .part emptied before the fresh start
        restart = max(i for i, x in enumerate(seen) if x < 500_000)
        self.assertLess(seen[restart], 500_000)                          # progress counted again from 0
        self.assertTrue(all(isinstance(x, tuple) or x <= len(self.model) for x in sizes))   # never longer than the file: no append

    def test_wrong_checksum(self):
        d = self.dl('model.gguf', sha256='0' * 64)
        with self.assertRaises(download.BadChecksum):
            d.run()
        self.assertFalse(Path(d.part).exists())

    def test_redirects(self):
        self.files.redirect['a'] = f'{self.base}/model.gguf'
        d = download.Download(f'{self.base}/a', self.tmp / 'x', len(self.model), sha(self.model), download.Control(), allowed=ALLOWED, context=self.ctx)
        self.assertEqual(Path(d.run()).read_bytes(), self.model)
        for where in ('https://evil.example/model.gguf', f'http://127.0.0.1:{self.srv.server_address[1]}/model.gguf'):
            self.files.redirect['b'] = where
            d = download.Download(f'{self.base}/b', self.tmp / 'y', 10, '0' * 64, download.Control(), allowed=ALLOWED, context=self.ctx)
            with self.assertRaises(download.Failed) as e:
                d.run()
            self.assertEqual(e.exception.code, 'network')
        self.files.redirect['c'] = f'{self.base}/c'
        d = download.Download(f'{self.base}/c', self.tmp / 'z', 10, '0' * 64, download.Control(), allowed=ALLOWED, context=self.ctx)
        with self.assertRaises(download.Failed):
            d.run()

    def test_refuses_unknown_host_before_connecting(self):
        d = download.Download('https://example.com/x', self.tmp / 'x', 10, '0' * 64, download.Control())
        with self.assertRaises(download.Failed):
            d.run()

    def test_untrusted_certificate(self):
        """A server whose chain ends outside roots.pem (this test's own CA) is refused at once, with the product's own context."""
        d = download.Download(f'{self.base}/model.gguf', self.tmp / 'x', len(self.model), sha(self.model), download.Control(), allowed=ALLOWED)
        t0 = time.time()
        with self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('could not confirm it is talking to the download server', e.exception.message)
        self.assertLess(time.time() - t0, 5)          # no retries

    def test_host_that_never_answers_is_unreachable(self):
        """A host reset before it answers, every time (huggingface.co on some networks, #254): retrying on this network will not
        help, so the download gives up as 'unreachable', naming the host, after the same tries as a broken connection."""
        blocked = ResetServer()
        self.addCleanup(blocked.close)
        d = download.Download(blocked.url('model.gguf'), self.tmp / 'dl' / 'model.gguf', len(self.model), sha(self.model),
                              download.Control(), allowed=ALLOWED, context=self.ctx)
        with fast_backoff(), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual((e.exception.code, e.exception.extra), ('unreachable', {'host': '127.0.0.1'}))
        self.assertIn('could not reach 127.0.0.1', e.exception.message)
        self.assertEqual(blocked.count, download.NETWORK_TRIES)

    def test_host_that_answered_then_broke_is_network(self):
        """A server that answers, then cuts every answer before a byte of the file, is reachable: that stays 'network'."""
        self.files.cut_after['model.gguf'] = [0] * (download.NETWORK_TRIES + 2)
        with fast_backoff(), self.assertRaises(download.Failed) as e:
            self.dl('model.gguf').run()
        self.assertEqual(e.exception.code, 'network')
        self.assertIn('kept breaking', e.exception.message)

    def test_roots_only(self):
        ctx = download.tls_context()
        self.assertEqual(ctx.cert_store_stats()['x509_ca'], 11)      # roots.pem and nothing from the system
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(ctx.check_hostname)
        text = download.roots_path().read_text()
        self.assertNotRegex(text.lower(), 'emudhra|emsign')
        with mock.patch.object(download, 'roots_path', lambda: Path('/nonexistent/roots.pem')), self.assertRaises(download.Failed):
            download.tls_context()

    def test_one_stray_200_does_not_restart(self):
        self.files.cut_after['model.gguf'] = [700_000]
        self.files.ignore_once['model.gguf'] = 1
        with fast_backoff():
            p = self.dl('model.gguf').run()
        self.assertEqual(Path(p).read_bytes(), self.model)
        self.assertEqual([r for _, r, _ in self.files.log], [None, 'bytes=700000-', 'bytes=700000-'])   # asked again, not from 0

    def test_pause_mid_file(self):
        self.files.pace = 0.01
        c = download.Control()
        d = self.dl('model.gguf', control=c)
        threading.Timer(0.1, c.stop, ['paused']).start()
        with self.assertRaises(download.Stopped):
            d.run()
        self.assertGreater(d.done_bytes(), 0)
        self.assertLess(d.done_bytes(), len(self.model))
        self.files.pace = 0
        d.control = download.Control()
        self.assertEqual(Path(d.run()).read_bytes(), self.model)


def pieces(n=4, piece=256 * 1024):
    """Several connections for a test-sized file: anything of 1 MiB or more, in pieces of `piece`."""
    return mock.patch.multiple(download, CONNECTIONS=n, PARALLEL_MIN=MiB, PIECE=piece, AHEAD=2 * n)


class ParallelDownloads(Base):
    def setUp(self):
        super().setUp()
        self.big = os.urandom(3 * MiB + 4321)
        self.files.data['big.bin'] = self.big

    def dl(self, sha256=None, size=None, control=None, connections=None):
        return download.Download(f'{self.base}/big.bin', self.tmp / 'dl' / 'big.bin', size or len(self.big), sha256 or sha(self.big),
                                 control or download.Control(), allowed=ALLOWED, context=self.ctx, connections=connections)

    def ranges(self):
        return [r for n, r, _ in self.files.log if n == 'big.bin']

    def test_pieces(self):
        seen = []
        d = self.dl()
        d.progress = lambda done, total, phase: seen.append(done)
        with pieces():
            p = d.run()
        self.assertEqual(Path(p).read_bytes(), self.big)
        self.assertFalse(Path(d.part).exists() or Path(d.meta).exists())
        r = self.ranges()
        self.assertEqual(len(r), 13)                                       # 3 MiB + a bit in 256 KiB pieces
        self.assertTrue(all(x and x.startswith('bytes=') and not x.endswith('-') for x in r), r)   # every one closed: bytes=a-b
        self.assertEqual(sorted(int(x[6:].split('-')[0]) for x in r), [i * 256 * 1024 for i in range(13)])
        self.assertEqual(seen[-1], len(self.big))
        self.assertTrue(all(b >= a for a, b in zip(seen, seen[1:])), 'progress went back')

    def test_small_file_one_connection(self):
        with pieces():
            p = download.Download(f'{self.base}/model.gguf', self.tmp / 'm', 1000, sha(self.model[:1000]), download.Control(),
                                  allowed=ALLOWED, context=self.ctx)
            self.files.data['model.gguf'] = self.model[:1000]
            p.run()
        self.assertEqual([r for n, r, _ in self.files.log if n == 'model.gguf'], [None])

    def test_one_connection_asked_for(self):
        with pieces():
            self.dl(connections=1).run()
        self.assertEqual(self.ranges(), [None])

    def test_cut_pieces_try_again(self):
        # Bind the faults to piece offsets, not thread arrival order. Piece zero must carry the mid-piece cut.
        self.files.cut_after['big.bin'] = {0: 200_000, 256 * 1024: 200_000, 512 * 1024: 200_000}
        with pieces(), fast_backoff():
            p = self.dl().run()
        self.assertEqual(Path(p).read_bytes(), self.big)
        self.assertEqual(len(self.ranges()), 16)
        self.assertIn('bytes=200000-262143', self.ranges())            # the cut piece carried on from where it broke

    def test_pause_and_resume(self):
        """Pause while one piece is held open by the server: every connection stops, nothing is written after run() returns,
        and the resume finishes from the hashed run. The server's hold, not a timer, decides when the pause lands."""
        hold = self.files.hold['big.bin'] = Hold(600_000)                # inside the third 256 KiB piece
        self.addCleanup(hold.release.set)
        c = download.Control()
        d = self.dl(control=c)
        out = []
        with pieces():
            t = threading.Thread(target=lambda: out.append(self.outcome(d.run)), name='test-run')
            t.start()
            self.assertTrue(hold.reached.wait(10), 'the held piece was never asked for')
            c.stop('paused')
            t.join(15)
        self.assertFalse(t.is_alive(), 'run() did not return after pause')
        self.assertIsInstance(out[0], download.Stopped)
        self.assertEqual(self.download_threads(), [], 'a download connection outlived the pause')
        before = self.snapshot(d.part)
        hold.release.set()                                               # the held answer sends the rest of its piece now
        time.sleep(0.5)
        self.assertEqual(self.snapshot(d.part), before, 'the partial file changed after pause returned')
        meta = json.loads(Path(d.meta).read_text())
        self.assertTrue(meta['parallel'])
        self.assertEqual(meta['bytes_done'] % (256 * 1024), 0)           # a whole number of pieces, hashed
        self.assertEqual(d.done_bytes(), meta['bytes_done'])
        self.assertLessEqual(meta['bytes_done'], os.path.getsize(d.part))
        self.assertLessEqual(meta['bytes_done'], 512 * 1024)              # the held piece was never in the hashed run
        self.files.log.clear()
        d.control = download.Control()
        with pieces():
            p = d.run()
        self.assertEqual(sha(Path(p).read_bytes()), sha(self.big))
        self.assertTrue(all(int(x[6:].split('-')[0]) >= meta['bytes_done'] for x in self.ranges()))   # nothing hashed is fetched again
        self.assertEqual(self.download_threads(), [])

    @staticmethod
    def outcome(fn):
        try:
            return fn()
        except BaseException as e:      # noqa: B036 -- handed back to the test thread to assert on
            return e

    @staticmethod
    def download_threads():
        return [t.name for t in threading.enumerate() if t.name.startswith('download-') and t.is_alive()]

    @staticmethod
    def snapshot(path):
        p = Path(path)
        return (p.stat().st_size, p.stat().st_mtime_ns, sha(p.read_bytes())) if p.exists() else None

    def test_pause_during_stalled_handshake(self):
        """A server that takes the connection but never answers the TLS handshake: pause ends the run and every worker within a
        couple of seconds, not at the 30 s network timeout."""
        lsn = socket.socket()
        lsn.bind(('127.0.0.1', 0))
        lsn.listen(8)
        held, accepted = [], threading.Semaphore(0)

        def accept():
            while True:
                try:
                    s, _ = lsn.accept()
                except OSError:
                    return
                held.append(s)
                accepted.release()
        listener = threading.Thread(target=accept, daemon=True)
        listener.start()
        self.files.redirect['big.bin'] = f'https://127.0.0.1:{lsn.getsockname()[1]}/big.bin'
        c = download.Control()
        d = self.dl(control=c, connections=2)
        err = []

        def run():
            try:
                with pieces():
                    d.run()
            except BaseException as e:      # noqa: B036
                err.append(e)
        runner = threading.Thread(target=run, daemon=True)
        try:
            runner.start()
            for _ in range(2):
                self.assertTrue(accepted.acquire(timeout=10), 'the workers never connected')
            time.sleep(0.3)                 # both are in the handshake now, waiting for a ServerHello that never comes
            t0 = time.monotonic()
            c.stop('paused')
            runner.join(10)
            took = time.monotonic() - t0
            self.assertFalse(runner.is_alive(), 'run() was still going 10 s after the pause')
            self.assertEqual(len(err), 1)
            self.assertIsInstance(err[0], download.Stopped)
            self.assertLess(took, 2)
            self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith('download-')], [])
        finally:
            try:
                lsn.shutdown(socket.SHUT_RDWR)   # wake accept() before closing the fixture listener
            except OSError:
                pass
            lsn.close()
            listener.join(1)
            for s in held:
                s.close()

    def test_resume_after_kill_ignores_unhashed_bytes(self):
        """Bytes past bytes_done may be holes or half a piece: a resume trusts only the hashed run."""
        d = self.dl()
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(self.big[:512 * 1024] + b'\0' * (1024 * 1024))
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"e1"', 'bytes_done': 512 * 1024, 'parallel': True}))
        with pieces():
            self.assertEqual(Path(d.run()).read_bytes(), self.big)
        self.assertEqual(min(int(x[6:].split('-')[0]) for x in self.ranges()), 512 * 1024)

    def test_resume_from_one_connection_file(self):
        """A .part left by one connection (no "parallel" in its .json) carries on in pieces from its length."""
        d = self.dl()
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(self.big[:700_000])
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"e1"', 'bytes_done': 600_000}))
        with pieces():
            self.assertEqual(Path(d.run()).read_bytes(), self.big)
        self.assertEqual(min(int(x[6:].split('-')[0]) for x in self.ranges()), 700_000)

    def test_etag_changed_starts_over(self):
        d = self.dl()
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(b'x' * 512 * 1024)
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"old"', 'bytes_done': 512 * 1024, 'parallel': True}))
        self.files.etag['big.bin'] = '"new"'
        with pieces():
            self.assertEqual(Path(d.run()).read_bytes(), self.big)
        self.assertIn('bytes=0-262143', self.ranges())

    def test_etag_changed_same_bytes_keeps_the_part(self):
        """The same over several connections: the hashed run is kept, and the pieces carry on from it (#208)."""
        d = self.dl()
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(self.big[:512 * 1024] + b'\0' * (300 * 1024))     # and a window past it, not vouched for
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"old"', 'bytes_done': 512 * 1024, 'parallel': True}))
        self.files.etag['big.bin'] = '"new"'
        with pieces():
            self.assertEqual(Path(d.run()).read_bytes(), self.big)
        r = self.ranges()
        self.assertNotIn('bytes=0-262143', r)                              # the first piece was not fetched again
        self.assertNotIn(None, r)
        self.assertIn(f'bytes={len(self.big) - 4321}-{len(self.big) - 1}', r)   # the last one came as a piece: still several connections

    def test_etag_changed_and_the_check_breaks_keeps_the_part(self):
        """The comparison could not be made (a busy server): it goes on over one connection, which waits and checks again."""
        d = self.dl()
        os.makedirs(os.path.dirname(d.part), exist_ok=True)
        Path(d.part).write_bytes(self.big[:512 * 1024])
        Path(d.meta).write_text(json.dumps({'url': d.url, 'size': d.size, 'sha256': d.sha256, 'etag': '"old"', 'bytes_done': 512 * 1024, 'parallel': True}))
        self.files.etag['big.bin'] = '"new"'
        real = download.Download._same_bytes

        def busy_once(self_, n, ctx):
            if not busy_once.done:
                busy_once.done = True
                self.files.answers['big.bin'] = [(503, None)]
            return real(self_, n, ctx)
        busy_once.done = False
        with pieces(), fast_backoff(), mock.patch.object(download.Download, '_same_bytes', busy_once):
            self.assertEqual(Path(d.run()).read_bytes(), self.big)
        r = self.ranges()
        self.assertNotIn(None, r)
        self.assertNotIn('bytes=0-262143', r)
        self.assertIn(f'bytes={512 * 1024}-', r)                           # carried on over one connection from the hashed run

    def test_server_ignores_range_goes_to_one_connection(self):
        self.files.ignore_range.add('big.bin')
        with pieces(), fast_backoff():
            p = self.dl().run()
        self.assertEqual(Path(p).read_bytes(), self.big)
        self.assertEqual(self.ranges()[-1], None)                          # finished as one plain GET
        self.assertNotIn('parallel', json.dumps(self.files.log))

    def test_one_stray_200_keeps_pieces(self):
        self.files.ignore_once['big.bin'] = 1
        with pieces():
            p = self.dl().run()
        self.assertEqual(Path(p).read_bytes(), self.big)
        self.assertEqual(len(self.ranges()), 14)                           # 13 pieces, one asked for twice
        self.assertNotIn(None, self.ranges())                              # never went back to one plain GET

    def test_wrong_checksum(self):
        d = self.dl(sha256='0' * 64)
        with pieces(), self.assertRaises(download.BadChecksum):
            d.run()
        self.assertFalse(Path(d.part).exists())

    def test_wrong_size_on_server(self):
        d = self.dl(size=len(self.big) + 10)
        with pieces(), self.assertRaises(download.BadChecksum):
            d.run()

    def test_unreachable_over_several_connections(self):
        """A large file from a host that never answers gives up as 'unreachable' over pieces too."""
        blocked = ResetServer()
        self.addCleanup(blocked.close)
        d = download.Download(blocked.url('big.bin'), self.tmp / 'dl' / 'big.bin', len(self.big), sha(self.big), download.Control(),
                              allowed=ALLOWED, context=self.ctx)
        with pieces(), fast_backoff(), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual((e.exception.code, e.exception.extra), ('unreachable', {'host': '127.0.0.1'}))

    def test_redirect_to_unknown_host_refused(self):
        self.files.redirect['big.bin'] = 'https://evil.example/big.bin'
        with pieces(), self.assertRaises(download.Failed) as e:
            self.dl().run()
        self.assertEqual(e.exception.code, 'network')

    # -------------------------------------------------------------- a busy server, over several connections (with #149) --
    def test_busy_pieces_wait_it_out(self):
        """30 answers of 503 or 429 are a busy server, not 30 broken connections: the pieces wait (and say so), then finish.
        Over one connection each piece would give up after NETWORK_TRIES."""
        self.files.answers['big.bin'] = [(503, None), (429, None)] * 15
        d, waits = self.dl(), []
        d.waiting = waits.append
        with pieces(n=2), fast_backoff():
            p = d.run()
        self.assertEqual(sha(Path(p).read_bytes()), sha(self.big))
        self.assertTrue(waits and all(w == 0.05 for w in waits), waits)

    def test_busy_pieces_share_one_budget(self):
        """The connections share one wait and one budget: four of them waiting together count the time once, and the download
        stops as 'server' (not 'network') once BUSY_LIMIT has been waited with no progress."""
        self.files.answers['big.bin'] = [(503, None)] * 400
        d, waits = self.dl(), []
        d.waiting = waits.append
        t0 = time.monotonic()
        with pieces(n=4), fast_backoff(), mock.patch.object(download, 'BUSY_LIMIT', 0.3), self.assertRaises(download.Failed) as e:
            d.run()
        self.assertEqual(e.exception.code, 'server')
        self.assertLessEqual(sum(waits), 0.3 + 1e-9)
        self.assertLessEqual(len(waits), 6)                                   # 0.3 s in waits of 0.05 s, not one set per connection
        self.assertLess(time.monotonic() - t0, 5)
        self.assertGreater(len(self.ranges()), len(waits))                    # several connections joined each wait

    def busy_clock(self, limit=20):
        """A _Pieces on a clock the test moves by hand (now[0], seconds): no sleeping, no server. BUSY_BACKOFF 10 s."""
        now = [0.0]
        for p in (mock.patch.object(download.time, 'monotonic', lambda: now[0]),
                  mock.patch.object(download, 'BUSY_BACKOFF', (10,) * 6), mock.patch.object(download, 'BUSY_LIMIT', limit)):
            p.start()
            self.addCleanup(p.stop)
        return now, download._Pieces(0, 10 * download.PIECE, None)

    def test_busy_budget_and_wait_rules(self):
        """The shared rules on a moved clock: one announced wait that others join without charging it again, Retry-After
        as a floor within what is left of the budget, the budget used up with no progress, and a byte renewing it."""
        now, st = self.busy_clock()
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (10, True))
        now[0] = 4
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (6, False))       # another connection joins: 6 s left
        self.assertEqual(st.waited, 10)                                          # counted once, not twice
        now[0] = 10                                                              # that wait is over
        self.assertEqual(st.busy_wait(download._Busy(503, '15')), (10, True))    # Retry-After 15, but only 10 s of 20 left
        now[0] = 20
        with self.assertRaises(download.Failed) as e:
            st.busy_wait(download._Busy(503, None))
        self.assertEqual(e.exception.code, 'server')
        st.progress(0, 1)                                                        # one byte arrived, no wait on: all 20 s again
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (10, True))

    def test_joining_answer_with_a_longer_retry_after_extends_the_wait(self):
        """A connection told "Retry-After: 15" one second into a 10 s wait must not try again before 16 s: the shared wait is
        extended to 16, only the 6 s extension is charged, and the countdown hears the new wait. Within the budget only."""
        now, st = self.busy_clock()
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (10, True))
        now[0] = 1
        self.assertEqual(st.busy_wait(download._Busy(429, '15')), (15, True))    # announced again: the wait got longer
        self.assertEqual((st.resume_at, st.waited, st.announce), (16, 16, 15))
        now[0] = 2
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (14, False))   # a shorter one just joins
        self.assertEqual(st.waited, 16)
        now, st = self.busy_clock(limit=12)                                       # only 2 s of budget left for the extension
        st.busy_wait(download._Busy(503, None))
        now[0] = 1
        self.assertEqual(st.busy_wait(download._Busy(429, '15')), (11, True))
        self.assertEqual((st.resume_at, st.waited), (12, 12))
        now[0] = 12
        with self.assertRaises(download.Failed):
            st.busy_wait(download._Busy(503, None))

    def test_busy_pieces_wait_for_the_longest_retry_after(self):
        """Over the test server: one connection hears 503 (a 0.05 s wait), the other "Retry-After: 1". No connection asks
        again until that second has passed, including the one already asleep on the short wait; then the file completes."""
        self.files.answers['big.bin'] = [(503, None), (503, 1)]
        times, real_open = [], download._open

        def timed_open(*a, **k):
            times.append(time.monotonic())
            return real_open(*a, **k)
        with pieces(n=2), fast_backoff(), mock.patch.object(download, '_open', timed_open):
            p = self.dl().run()
        self.assertEqual(sha(Path(p).read_bytes()), sha(self.big))
        later = [t - times[1] for t in times[2:]]
        self.assertTrue(later and min(later) >= 0.9, [round(x, 2) for x in later[:4]])

    def test_progress_during_a_wait_does_not_forgive_the_rest_of_it(self):
        """Bytes on another piece one second into a 10 s wait renew the budget, but the 9 s still to wait come after that
        progress and count: with a 20 s budget the download gives up 20 s after the progress, not 29."""
        now, st = self.busy_clock()
        st.busy_wait(download._Busy(503, None))
        now[0] = 1
        st.progress(0, 1)
        self.assertEqual((st.busy, st.waited), (0, 9))
        now[0] = 10
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (10, True))
        now[0] = 20
        self.assertEqual(st.busy_wait(download._Busy(503, None)), (1, True))     # 19 of 20 s used since the progress
        now[0] = 21
        with self.assertRaises(download.Failed) as e:
            st.busy_wait(download._Busy(503, None))
        self.assertEqual(e.exception.code, 'server')
        self.assertLessEqual(now[0] - 1, download.BUSY_LIMIT)

    def test_pause_ends_a_busy_wait_and_a_stalled_handshake_together(self):
        """#125 with #149: one connection asleep in a busy wait (Retry-After 300), the other stuck in a TLS handshake that never
        answers. Pause ends both within about 2 s, raises Stopped, and leaves no download-* thread."""
        lsn = socket.socket()
        lsn.bind(('127.0.0.1', 0))
        lsn.listen(8)
        held, accepted = [], threading.Event()

        def accept():
            while True:
                try:
                    s, _ = lsn.accept()
                except OSError:
                    return
                held.append(s)
                accepted.set()
        listener = threading.Thread(target=accept, daemon=True)
        listener.start()
        stall = f'https://127.0.0.1:{lsn.getsockname()[1]}/big.bin'
        self.files.answers['big.bin'] = [(503, 300)]
        calls, real_open, lock = [], download._open, threading.Lock()

        def split_open(url, *a, **k):
            with lock:
                calls.append(url)
                n = len(calls)
            return real_open(stall if n == 2 else url, *a, **k)     # the second connection goes to the silent listener
        c = download.Control()
        d, waits, err = self.dl(control=c, connections=2), [], []
        d.waiting = waits.append

        def run():
            try:
                with pieces(n=2), mock.patch.object(download, '_open', split_open):
                    d.run()
            except BaseException as e:      # noqa: B036
                err.append(e)
        runner = threading.Thread(target=run, daemon=True)
        try:
            runner.start()
            self.assertTrue(accepted.wait(10), 'no connection reached the silent listener')
            t0 = time.monotonic()
            while not waits and time.monotonic() - t0 < 10:
                time.sleep(0.02)
            self.assertEqual(waits[:1], [300])                              # the busy wait is on, and announced
            time.sleep(0.3)
            t0 = time.monotonic()
            c.stop('paused')
            runner.join(10)
            took = time.monotonic() - t0
            self.assertFalse(runner.is_alive(), 'run() was still going 10 s after the pause')
            self.assertEqual(len(err), 1)
            self.assertIsInstance(err[0], download.Stopped)
            self.assertLess(took, 2)
            self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith('download-')], [])
        finally:
            try:
                lsn.shutdown(socket.SHUT_RDWR)   # wake accept() before closing the fixture listener
            except OSError:
                pass
            lsn.close()
            listener.join(1)
            for s in held:
                s.close()

    def test_job_countdown_while_pieces_wait_then_pause_and_resume(self):
        """Through api.Setup: while every connection waits on a busy server the item shows one countdown, which the coordinator's
        polling does not wipe; Pause during that wait returns at once; resume finishes."""
        self.files.answers['Lastbook-Small.zip'] = [(503, 300)] * 50
        with pieces():
            s = self.setup()
            s.start('small', False)
            t0, seen = time.time(), []
            while time.time() - t0 < 10 and not seen:
                it = s.public_job()['items'][0]
                if it.get('retry_in_s'):
                    seen.append(it['retry_in_s'])
                time.sleep(0.02)
            self.assertTrue(seen, 'no countdown shown')
            self.assertTrue(295 <= seen[0] <= 300, seen)
            time.sleep(1.5)                                                # three coordinator polls later, still counting down
            it = s.public_job()['items'][0]
            self.assertIn('retry_in_s', it)
            self.assertTrue(293 <= it['retry_in_s'] < seen[0], (it, seen))
            self.assertEqual((it['rate_bps'], it['eta_s']), (0, None))
            t1 = time.monotonic()
            j = s.pause()
            self.assertLess(time.monotonic() - t1, 3)
            self.assertEqual(j['state'], 'paused')
            self.assertNotIn('retry_in_s', j['items'][0])
            self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith('download-')], [])
            self.files.answers.clear()
            s.resume()
            self.assertEqual(self.wait(s)['state'], 'done')


class Unpacking(Base):
    def bad_zip(self, info, data=b'x'):
        p = self.tmp / 'bad.zip'
        make_pack(p, {'library-data/catalog.json': b'{}'}, [(info, data)])
        return p

    def test_guards(self):
        cases = ['LastbookData/../../escape.txt', '/etc/evil', 'Other/x', 'LastbookData\\..\\x', 'C:/x']
        link = zipfile.ZipInfo('LastbookData/link')
        link.external_attr = (0o120777 << 16)
        for case in cases + [link]:
            p = self.bad_zip(case)
            with self.assertRaises(install.Refused, msg=str(case)):
                install.unpack(p, self.tmp / 'out', 10 * MiB, Control(), lambda d, t: None)
        self.assertFalse((self.tmp / 'escape.txt').exists())
        self.assertFalse((self.tmp / 'out' / 'LastbookData' / 'library-data').exists())   # nothing written: every member is checked first

    def test_compression_and_overlap(self):
        p = self.tmp / 'bz.zip'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('LastbookData/library-data/catalog.json', b'{}', compress_type=zipfile.ZIP_BZIP2)
        with self.assertRaises(install.Refused):
            install.check_zip(p)
        ok = self.tmp / 'ok.zip'
        with zipfile.ZipFile(ok, 'w') as z:
            z.writestr('LastbookData/library-data/catalog.json', b'{}' * 1000)
            z.writestr('LastbookData/library-data/b.bin', b'x' * 5000)
        install.check_zip(ok)                       # a normal zip passes
        data = bytearray(ok.read_bytes())           # point the second entry's central record at the first one's bytes
        cd = data.rfind(b'PK\x01\x02')
        data[cd + 42:cd + 46] = (0).to_bytes(4, 'little')
        bad = self.tmp / 'overlap.zip'
        bad.write_bytes(bytes(data))
        with self.assertRaises(install.Refused):
            install.check_zip(bad)

    def test_member_stops_at_its_size(self):
        p = self.tmp / 'Lastbook-Small.zip'
        real = zipfile.ZipFile.open

        def longer(self_, info, *a, **k):          # a member that yields more bytes than it declares
            f = real(self_, info, *a, **k)
            extra = [b'more than it says']
            r = f.read
            f.read = lambda n=-1: r(n) or (extra.pop() if extra else b'')
            return f
        with mock.patch.object(zipfile.ZipFile, 'open', longer), self.assertRaises(install.Refused):
            install.unpack(p, self.tmp / 'out', self.installed, Control(), lambda d, t: None)

    def test_size_check(self):
        p = self.tmp / 'Lastbook-Small.zip'
        with self.assertRaises(install.Refused):
            install.unpack(p, self.tmp / 'out', self.installed // 2, Control(), lambda d, t: None)

    def test_resume_skips_whole_files(self):
        p = self.tmp / 'Lastbook-Small.zip'
        out = self.tmp / 'out'
        lib = install.unpack(p, out, self.installed, Control(), lambda d, t: None)
        big = lib / 'library-data' / 'big.bin'
        t = big.stat().st_mtime_ns
        (lib / 'derived' / 'geo' / 'map.sqlite').unlink()
        (lib / 'derived' / 'geo' / 'map.sqlite.partial').write_bytes(b'half')
        install.unpack(p, out, self.installed, Control(), lambda d, t: None)
        self.assertEqual(big.stat().st_mtime_ns, t)
        self.assertEqual((lib / 'derived' / 'geo' / 'map.sqlite').stat().st_size, MiB)


class SplitPacks(Base):
    def split_manifest(self):
        folder = self.tmp / 'fixture'
        catalog = multipart_fixture.write_fixture(folder)
        pack = catalog['packs'][0]
        parts = pack['parts']
        for file in pack['files'] + parts:
            name = file['name']
            file['url'] = f'{self.base}/{name}'
            self.files.data[name] = (folder / name).read_bytes()
        self.zip = self.files.data['Lastbook-Small.zip']
        (self.tmp / 'Lastbook-Small.zip').write_bytes(self.zip)
        bundled = manifest.validate(catalog, ALLOWED, bundled=True)
        return manifest.Manifest(bundled=bundled, remote=None, allowed=ALLOWED, context=self.ctx), parts

    def test_only_bundled_catalog_can_supply_parts(self):
        catalog, parts = self.split_manifest()
        fetched = manifest.validate(catalog.current, ALLOWED)
        self.assertNotIn('parts', fetched['packs'][0])
        self.assertNotIn('parts', catalog.public()['packs'][0])
        invalid = {**catalog.current, 'packs': [{**catalog.current['packs'][0], 'parts': parts[::-1][1:]}]}
        self.assertIsNone(manifest.validate(invalid, ALLOWED, bundled=True))

    def test_resumes_split_pack_after_a_missing_part(self):
        catalog, parts = self.split_manifest()
        original = 'Lastbook-Small.zip'
        del self.files.data[original]
        absent = parts[-1]['name']
        tail = self.files.data.pop(absent)
        s = self.setup(manifest=catalog)
        s.start('small')
        self.assertEqual(self.wait(s)['error']['code'], 'network')
        work = self.parent / 'LastbookData.setup' / 'downloads' / 'pack-parts'
        first, second = (work / part['name'] for part in parts[:2])
        self.assertEqual(first.read_bytes(), self.zip[:parts[0]['size']])
        good = second.read_bytes()
        second.write_bytes(bytes([good[0] ^ 1]) + good[1:])
        self.files.data[absent] = tail
        del self.files.data[first.name]  # the verified saved part must be reusable even if its URL is unavailable

        self.files.log.clear()
        restarted = self.setup(manifest=catalog)
        restarted.retry()
        job = self.wait(restarted)
        self.assertEqual(job['state'], 'done', job)
        library, note = api.startup(self.settings)
        self.assertFalse(note.get('error'))
        self.assertNotIn(first.name, [request[0] for request in self.files.log])
        self.assertEqual((library / 'library-data' / 'example.txt').read_bytes(), b'A small offline library.\n')

    def test_original_zip_import_still_works_with_a_split_pack(self):
        catalog, _ = self.split_manifest()
        self.files.data.clear()
        source = self.tmp / 'Lastbook-Small.zip'
        s = self.setup(manifest=catalog)
        s.use(str(source))
        job = self.wait(s)
        self.assertEqual(job['state'], 'done', job)
        self.assertEqual(source.read_bytes(), self.zip)


class Jobs(Base):
    def test_first_run_end_to_end(self):
        s = self.setup()
        st = s.state()
        self.assertEqual(st['mode'], 'first-run')
        self.assertEqual(st['recommend']['pack'], 'small')
        self.assertNotIn('url', json.dumps(st['manifest']))
        j = s.start('small', True)
        self.assertIsNone(j.get('error'), j)
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual([i['state'] for i in j['items']], ['done', 'done'])
        work = self.parent / 'LastbookData.setup'
        self.assertTrue(api._pending_file(self.settings).is_file())         # in the app's settings, not the work folder
        self.assertFalse((work / install.PENDING).exists())
        self.assertFalse((work / 'downloads' / 'Lastbook-Small.zip').exists())   # deleted once unpacked
        self.assertFalse((self.parent / 'LastbookData').exists())               # nothing in place until the switch
        finished = []
        s.on_finish = finished.append
        self.assertEqual(s.finish()['folder'], str(self.parent / 'LastbookData'))
        time.sleep(0.6)
        self.assertEqual(finished, [str(self.parent / 'LastbookData')])
        lib, note = api.startup(self.settings)                     # what main.py does on the next start
        self.assertEqual(lib, self.parent / 'LastbookData')
        self.assertTrue((lib / 'library-data' / 'catalog.json').is_file())
        self.assertEqual((lib / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertEqual(note['model_file'], str(lib / 'models' / 'model.gguf'))
        with mock.patch.dict(sys.modules, {'llm_local': self.llm}):
            api.opened(self.settings, lib, note)
        self.assertEqual(self.marked, [(str(lib / 'models' / 'model.gguf'), sha(self.model))])   # the digest setup matched, carried through the switch
        self.assertFalse(work.exists())
        self.assertEqual(json.loads((lib / 'setup-pack.json').read_text())['pack'], 'small')

    def test_choosing_another_pack_after_a_failure_installs_only_its_files(self):
        """#73: the first pack's unpacked files must not end up in the library made from the second."""
        s, shared = self.small_failed_then_medium()
        s.start('medium', False)                       # "Choose again", the same place
        self.assert_only_medium(s, shared)

    def test_a_folder_that_cannot_be_cleared_stops_until_it_can(self):
        """#86 review: if the old pack's files cannot be removed, setup stops with a retry message, and the retry installs
        only the new pack's files."""
        s, shared = self.small_failed_then_medium()
        real = shutil.rmtree

        def rmtree(path, ignore_errors=False, **k):       # like Windows with a file still open: the rest goes, that file stays
            if Path(path).name == 'unpack':
                for f in sorted(Path(path).rglob('*'), reverse=True):
                    if f.name != 'shared.bin':
                        f.rmdir() if f.is_dir() and not any(f.iterdir()) else f.unlink() if f.is_file() else None
                if not ignore_errors:
                    raise PermissionError('in use')
                return
            return real(path, ignore_errors=ignore_errors, **k)
        with mock.patch.object(api.shutil, 'rmtree', rmtree):
            s.start('medium', False)
            j = self.wait(s)
        self.assertEqual(j['state'], 'failed', j)
        self.assertIn('could not clear the files of the previous download', j['items'][0]['error']['message'])
        note = self.parent / 'LastbookData.setup' / 'unpack' / 'unpacked-from.json'
        self.assertFalse(note.exists() and json.loads(note.read_text())['pack'] == 'medium')
        s.start('medium', False)                       # "Try again", once the file is free
        self.assert_only_medium(s, shared)

    def assert_only_medium(self, s, shared):
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        s.finish()
        lib, _ = api.startup(self.settings)
        self.assertEqual((lib / 'pack.json').read_bytes(), b'{"name": "medium"}')
        self.assertEqual((lib / shared).read_bytes(), b'b' * MiB)
        self.assertFalse((lib / 'library-data' / 'only-small.bin').exists())

    def small_failed_then_medium(self):
        """A failed Small install has left its unpacked files; Medium is offered too."""
        shared = 'library-data/shared.bin'
        first = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(first, {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small"}',
                                           shared: b'a' * MiB, 'library-data/only-small.bin': b'small'})
        second = self.tmp / 'Lastbook-Medium.zip'
        medium_installed = make_pack(second, {'library-data/catalog.json': b'{"records": [1]}', 'pack.json': b'{"name": "medium"}',
                                              shared: b'b' * MiB})
        self.zip = first.read_bytes()
        medium_zip = second.read_bytes()
        self.files.data['Lastbook-Small.zip'] = self.zip
        self.files.data['Lastbook-Medium.zip'] = medium_zip
        medium = {'id': 'medium', 'title': 'Medium', 'summary': 'm', 'records': 4, 'installed_bytes': medium_installed, 'available': True,
                  'files': [{'name': 'Lastbook-Medium.zip', 'url': f'{self.base}/Lastbook-Medium.zip', 'size': len(medium_zip),
                             'sha256': sha(medium_zip), 'kind': 'zip'}]}
        m = self.manifest(more_packs=[medium])

        self.files.corrupt['model.gguf'] = 5          # the pack unpacks, then the local AI fails
        s = self.setup(manifest=m)
        s.start('small', True)
        j = self.wait(s)
        self.assertEqual(j['state'], 'failed', j)
        self.assertEqual([i['state'] for i in j['items']], ['done', 'failed'])
        return s, shared

    def test_trying_the_same_pack_again_keeps_its_unpacked_files(self):
        """#73: the reverse case: the same pack and revision still reuse what the failed run unpacked."""
        self.files.corrupt['model.gguf'] = 5
        s = self.setup()
        s.start('small', True)
        self.assertEqual(self.wait(s)['state'], 'failed')
        big = self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData' / 'library-data' / 'big.bin'
        before = big.stat().st_ino
        self.files.corrupt.clear()
        s.start('small', True)
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(big.stat().st_ino, before)    # not written again

    def test_corrupt_once_then_good(self):
        self.files.corrupt['Lastbook-Small.zip'] = 1
        s = self.setup()
        s.start('small', False)
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(len([1 for n, _, _ in self.files.log if n == 'Lastbook-Small.zip']), 2)

    def test_corrupt_twice_fails_checksum(self):
        s = self.setup(manifest=self.manifest(zip_sha='1' * 64))
        s.start('small', False)
        j = self.wait(s)
        self.assertEqual(j['state'], 'failed')
        self.assertEqual(j['error']['code'], 'checksum')
        self.assertEqual(len([1 for n, _, _ in self.files.log if n == 'Lastbook-Small.zip']), 2)
        self.assertFalse((self.parent / 'LastbookData').exists())

    def test_network_failure_then_retry(self):
        self.files.data.pop('model.gguf')
        s = self.setup()
        s.start('small', True)
        j = self.wait(s)
        self.assertEqual(j['state'], 'failed')
        self.assertEqual(j['error']['code'], 'network')
        self.assertEqual([i['state'] for i in j['items']], ['done', 'failed'])
        self.files.data['model.gguf'] = self.model
        s.retry()
        self.assertEqual(self.wait(s)['state'], 'done')

    def unreachable_model(self):
        """A first run whose library downloads and whose local AI is on a host this network cannot reach (#254)."""
        blocked = ResetServer()
        self.addCleanup(blocked.close)
        s = self.setup(manifest=self.manifest(model_url=blocked.url('model.gguf')))
        with fast_backoff():
            s.start('small', True)
            j = self.wait(s)
        return s, j, blocked

    def test_unreachable_model_says_so_with_the_file(self):
        """The stop names the host and carries what is needed to fetch the file another way: its name, size, address and
        SHA-256. The library is installed; only the AI waits."""
        s, j, blocked = self.unreachable_model()
        self.assertEqual((j['state'], [i['state'] for i in j['items']]), ('failed', ['done', 'failed']))
        self.assertEqual(j['error'], {'code': 'unreachable', 'message': 'Lastbook could not reach 127.0.0.1 from this network.',
                                      'host': '127.0.0.1', 'name': 'model.gguf', 'size': len(self.model),
                                      'url': blocked.url('model.gguf'), 'sha256': sha(self.model)})
        self.assertEqual(j['items'][1]['error'], j['error'])
        self.assertTrue((self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData' / 'library-data' / 'catalog.json').exists())   # unpacked, kept

    def test_unreachable_model_then_the_file_chosen(self):
        """Fetched elsewhere and chosen (use), the file goes to the stopped job: it is checked, the AI is installed, and nothing
        more is asked of the blocked host."""
        s, j, blocked = self.unreachable_model()
        tries = blocked.count
        got = self.home / 'Downloads' / 'model.gguf'
        got.write_bytes(self.model)
        r = s.use(str(got))
        self.assertIn(r['state'], ('waiting', 'verifying', 'activating', 'done'))
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(blocked.count, tries)
        self.assertEqual(s.finish()['folder'], str(self.parent / 'LastbookData'))
        time.sleep(0.6)
        lib, note = api.startup(self.settings)
        self.assertEqual((lib / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertEqual(note['model_file'], str(lib / 'models' / 'model.gguf'))

    def test_unreachable_model_with_part_already_downloaded_then_the_file_chosen(self):
        """#294 review: a retry that stops as 'unreachable' after an earlier try had downloaded part of the model. The card offers
        Choose AI model file there too, so the API must take the chosen file for that job as well (it used to only when no byte had
        come). The partial download is not used: the chosen file is checked and installed."""
        s, j, blocked = self.unreachable_model()
        part = Path(s.job['plan']['work']) / 'downloads' / 'model.gguf.part'
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(self.model[:1000])                              # what an earlier try had got
        with s.lock:
            s.job['items'][1]['bytes_done'] = 1000                       # the saved progress the card is drawn from
        tries = blocked.count
        got = self.home / 'Downloads' / 'model.gguf'
        got.write_bytes(self.model)
        r = s.use(str(got))
        self.assertIsNone(r.get('error'), r)
        self.assertNotEqual(r['state'], 'failed', r)                     # the job carries on with the chosen file
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(blocked.count, tries)
        self.assertEqual(s.finish()['folder'], str(self.parent / 'LastbookData'))
        time.sleep(0.6)
        lib, _ = api.startup(self.settings)
        self.assertEqual((lib / 'models' / 'model.gguf').read_bytes(), self.model)

    def test_a_network_stop_with_part_downloaded_is_left_to_retry(self):
        """Unchanged from #260: a 'network' stop after some of the model came is a dropped connection. The card offers Retry, not
        the picker, and choosing the file there does not resume that job."""
        self.files.cut_after['model.gguf'] = [700_000] + [0] * (download.NETWORK_TRIES + 2)
        s = self.setup()
        with fast_backoff():
            s.start('small', True)
            j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'network'), j)
        self.assertGreater(j['items'][1]['bytes_done'], 0)
        got = self.home / 'Downloads' / 'model.gguf'
        got.write_bytes(self.model)
        s.use(str(got))
        self.assertEqual(s.public_job()['state'], 'failed')

    def test_unreachable_model_wrong_file_chosen(self):
        """A file of another size is refused at once; one of the right size but other bytes stops as found_mismatch and is left
        where it is."""
        s, j, blocked = self.unreachable_model()
        short = self.home / 'Downloads' / 'model.gguf'
        short.write_bytes(self.model[:-1])
        r = s.use(str(short))
        self.assertEqual(r['code'], 'size')
        self.assertIn(f'{len(self.model) - 1:,} bytes, not the {len(self.model):,}', r['error'])
        self.assertEqual(s.public_job()['error']['code'], 'unreachable')      # the stop is unchanged
        short.write_bytes(b'x' * len(self.model))
        s.use(str(short))
        j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'found_mismatch'))
        self.assertEqual(short.read_bytes(), b'x' * len(self.model))

    def test_pause_resume_cancel(self):
        self.files.pace = 0.01
        s = self.setup()
        s.start('small', False)
        time.sleep(0.3)
        j = s.pause()
        self.assertEqual(j['state'], 'paused')
        done = j['items'][0]['bytes_done']
        self.assertGreater(done, 0)
        s.resume()
        time.sleep(0.3)
        j = s.public_job()
        self.assertEqual(j['state'], 'downloading')
        self.assertGreater(j['items'][0]['bytes_done'], done)
        self.assertIn(self.files.log[-1][1].split('-')[0], [f'bytes={n}' for n in range(done - 10 ** 6, done + 10 ** 7)])
        j = s.cancel()
        self.assertEqual(j['error']['code'], 'cancelled')
        self.assertFalse((self.parent / 'LastbookData.setup' / 'downloads').exists())
        self.assertFalse((self.parent / 'LastbookData').exists())

    def test_job_shows_busy_wait(self):
        """While the server is busy, the item says when the next try is, with no rate. Pausing then drops it, and resume finishes."""
        self.files.cut_after['Lastbook-Small.zip'] = [700_000]
        self.files.answers['Lastbook-Small.zip'] = [None, (503, 300)]
        s = self.setup()
        s.start('small', False)
        t0 = time.time()
        while time.time() - t0 < 10 and not s.public_job()['items'][0].get('retry_in_s'):
            time.sleep(0.02)
        it = s.public_job()['items'][0]
        self.assertEqual((it['state'], it['rate_bps'], it['eta_s']), ('downloading', 0, None))
        self.assertTrue(295 <= it['retry_in_s'] <= 300, it['retry_in_s'])
        j = s.pause()
        self.assertEqual(j['state'], 'paused')
        self.assertNotIn('retry_in_s', j['items'][0])
        self.assertEqual(j['items'][0]['bytes_done'], 700_000)
        s.resume()
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual([r for n, r, _ in self.files.log if n == 'Lastbook-Small.zip'][-1], 'bytes=700000-')

    def test_rate_and_eta(self):
        self.files.pace = 0.06
        s = self.setup()
        s.start('small', False)
        time.sleep(1.8)
        it = s.public_job()['items'][0]
        s.cancel()
        self.assertGreater(it['rate_bps'], 0)
        self.assertIsNotNone(it['eta_s'])

    def test_windows_folder_too_deep(self):
        self.assertFalse(api.path_too_deep(r'C:\Users\you\Documents\LastbookData', windows=True))
        self.assertTrue(api.path_too_deep('C:\\' + 'x' * 60 + '\\LastbookData', windows=True))
        self.assertFalse(api.path_too_deep('/' + 'x' * 200 + '/LastbookData', windows=False))
        s = self.setup()
        with mock.patch.object(api, 'path_too_deep', lambda t: True):
            r = s.start('small', False)
        self.assertEqual(r['code'], 'folder')
        self.assertIn('characters', r['error'])
        self.assertIsNone(s.public_job())

    def test_no_space_refusal(self):
        s = self.setup()
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 ** 9, 10 ** 12)):
            r = s.start('small', True)
        self.assertEqual(r['code'], 'no_space')
        self.assertGreater(r['need_bytes'], r['free_bytes'])
        self.assertIn('GB', r['error'])
        self.assertIsNone(s.public_job())

    def test_discovery_only_reads_owned_or_selected_library(self):
        lib = self.home / 'Downloads' / 'LastbookData'
        (lib / 'models').mkdir(parents=True)
        (lib / 'library-data').mkdir()
        (lib / 'library-data' / 'catalog.json').write_text('{}')
        (lib / 'models' / 'model.gguf').write_bytes(self.model)
        for name in ('Downloads', 'Documents', 'Desktop'):
            (self.home / name / 'Lastbook-Small.zip').write_bytes(self.zip)
        original_stat, original_iterdir, original_open = Path.stat, Path.iterdir, Path.open
        protected = [self.home / name for name in ('Downloads', 'Documents', 'Desktop')]

        def guard(path):
            if path == lib or lib in path.parents:
                return
            self.assertFalse(any(path == root or root in path.parents for root in protected), str(path))

        def stat(path, *args, **kwargs):
            guard(path)
            return original_stat(path, *args, **kwargs)

        def iterdir(path):
            guard(path)
            return original_iterdir(path)

        fresh, selected = self.setup(), self.setup(mode='manage', current=lib)
        def read(path, *args, **kwargs):
            guard(path)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'stat', stat), mock.patch.object(Path, 'iterdir', iterdir), mock.patch.object(Path, 'open', read):
            self.assertEqual(fresh.found(), [])
            self.assertEqual([f['path'] for f in selected.found()], [str(lib / 'models' / 'model.gguf')])

    def test_explicit_model_file_is_used(self):
        model = self.home / 'Downloads' / 'model.gguf'
        model.write_bytes(self.model)
        s = self.setup()
        self.assertTrue(s.use(str(model))['noted'])
        self.assertEqual([f['path'] for f in s.found()], [str(model)])
        s.start('small', True)
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertNotIn('model.gguf', [name for name, _, _ in self.files.log])
        self.assertEqual((self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)

    def model_download_stopped(self, managed=False):
        if managed:
            lib = self.parent / 'LastbookData'
            (lib / 'library-data').mkdir(parents=True)
            (lib / 'library-data' / 'catalog.json').write_text('{}')
            (lib / 'pack.json').write_text('{"name": "small"}')
            s = self.setup('manage', current=lib)
        else:
            s = self.setup()
        run = download.Download.run

        def blocked(d):
            if d.url.endswith('/model.gguf'):
                raise download.Failed('network', 'The connection kept breaking.')
            return run(d)

        with mock.patch.object(download.Download, 'run', blocked):
            s.start('small', True)
            j = self.wait(s)
        self.assertEqual(j['error']['code'], 'network')
        self.assertEqual([it['state'] for it in j['items']], ['failed'] if managed else ['done', 'failed'])
        return s, j

    def test_choose_model_after_network_failure_keeps_finished_pack(self):
        s, stopped = self.model_download_stopped()
        model = self.home / 'Downloads' / 'model.gguf'
        model.write_bytes(self.model)
        pack = Path(s.job['plan']['work']) / 'unpack' / 'LastbookData' / 'library-data' / 'catalog.json'
        before = pack.stat().st_mtime_ns
        s.use(str(model))
        j = self.wait(s)
        self.assertEqual(j['state'], 'done')
        self.assertEqual(j['id'], stopped['id'])
        self.assertEqual(pack.stat().st_mtime_ns, before)
        self.assertEqual([name for name, _, _ in self.files.log], ['Lastbook-Small.zip'])
        installed = pack.parents[1] / 'models' / 'model.gguf'
        self.assertEqual(installed.read_bytes(), self.model)
        self.assertEqual(json.loads(api._pending_file(self.settings).read_text())['model_sha256'], sha(self.model))

    def test_choose_model_after_network_failure_in_the_open_library(self):
        s, stopped = self.model_download_stopped(managed=True)
        model = self.home / 'Downloads' / 'model.gguf'
        model.write_bytes(self.model)
        s.use(str(model))
        j = self.wait(s)
        self.assertEqual((j['state'], j['id']), ('done', stopped['id']))
        installed = self.parent / 'LastbookData' / 'models' / 'model.gguf'
        self.assertEqual(installed.read_bytes(), self.model)
        self.assertEqual(self.marked, [(str(installed), sha(self.model))])
        self.assertEqual(self.files.log, [])

    def test_choose_model_after_network_failure_checks_the_hash(self):
        s, stopped = self.model_download_stopped()
        model = self.home / 'Downloads' / 'model.gguf'
        bad = bytes([self.model[0] ^ 1]) + self.model[1:]
        model.write_bytes(bad)
        s.use(str(model))
        j = self.wait(s)
        self.assertEqual(j['error']['code'], 'found_mismatch')
        self.assertEqual(j['id'], stopped['id'])
        self.assertEqual(model.read_bytes(), bad)
        self.assertEqual([name for name, _, _ in self.files.log], ['Lastbook-Small.zip'])

    def test_choose_incomplete_model_leaves_network_failure_alone(self):
        s, stopped = self.model_download_stopped()
        model = self.home / 'Downloads' / 'model.gguf'
        model.write_bytes(self.model[:100])
        self.assertEqual(s.use(str(model))['code'], 'size')
        self.assertEqual(s.public_job(), stopped)

    def test_found_zip_is_used_without_network(self):
        self.parent.mkdir()
        (self.parent / 'Lastbook-Small.zip').write_bytes(self.zip)
        s = self.setup()
        f = [x for x in s.found() if x['kind'] == 'pack']
        self.assertEqual(len(f), 1)
        self.assertTrue(f[0]['size_ok'])
        j = s.use(f[0]['path'])
        self.assertIsNone(j.get('error'), j)
        self.assertEqual(j['items'][0]['source'], 'found')
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(self.files.log, [])
        self.assertTrue((self.parent / 'Lastbook-Small.zip').exists())   # the person's own file stays

    def test_found_zip_wrong_contents_downloads_instead(self):
        bad = bytearray(self.zip)
        bad[100] ^= 0xff
        (self.home / 'Downloads' / 'Lastbook-Small.zip').write_bytes(bad)
        s = self.setup()
        s.start('small', False)
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(len(self.files.log), 1)
        bad_explicit = self.setup()
        (self.parent / 'LastbookData.setup').exists() and shutil.rmtree(self.parent / 'LastbookData.setup')
        mine = self.home / 'Downloads' / 'Lastbook-Small.zip'
        before = (mine.stat().st_size, mine.stat().st_mtime_ns, sha(mine.read_bytes()))
        bad_explicit.use(str(mine))
        j = self.wait(bad_explicit)
        self.assertEqual(j['error']['code'], 'found_mismatch')      # not 'checksum': nothing was downloaded
        self.assertEqual(j['error']['file'], str(mine))
        self.assertIn('Lastbook-Small.zip in Downloads doesn’t match the published file', j['error']['message'])
        self.assertEqual((mine.stat().st_size, mine.stat().st_mtime_ns, sha(mine.read_bytes())), before)   # the person's file, untouched
        f = next(x for x in bad_explicit.found() if x['path'] == str(mine))
        self.assertTrue(f['rejected'])
        self.assertFalse(f['size_ok'])                                # no longer offered as usable
        self.files.log.clear()
        bad_explicit.retry()                                          # "Download it"
        self.assertEqual(self.wait(bad_explicit)['state'], 'done')
        self.assertEqual([n for n, _, _ in self.files.log], ['Lastbook-Small.zip'])
        self.assertEqual((mine.stat().st_size, mine.stat().st_mtime_ns), before[:2])

    def test_use_existing_library(self):
        lib = self.tmp / 'elsewhere' / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        (lib / 'library-data' / 'catalog.json').write_text('{}')
        s = self.setup(look_for_libraries=lambda: [lib])
        self.assertIn(str(lib), [f['path'] for f in s.found() if f['kind'] == 'library'])
        self.assertEqual(s.use(str(lib.parent))['state'], 'done')
        self.assertEqual(s.finish()['folder'], str(lib))
        (self.tmp / 'empty').mkdir()
        self.assertEqual(s.use(str(self.tmp / 'empty'))['code'], 'not_library')
        s.job = None
        self.assertEqual(s.use(str(self.tmp))['items'][0]['kind'], 'pack')     # a folder holding a pack zip: that zip is used
        self.wait(s)

    def test_switch_to_another_library_while_one_is_open(self):
        """Library and local AI can open another LastbookData, say on a USB stick, but not the one that is open (#266)."""
        def library(where):
            lib = self.tmp / where / 'LastbookData'
            (lib / 'library-data').mkdir(parents=True)
            (lib / 'library-data' / 'catalog.json').write_text('{}')
            return lib
        here, usb = library('laptop'), library('usb')
        s = self.setup('manage', current=here)
        opened = []
        s.on_finish = opened.append
        for chosen in (here, here.parent):
            with self.subTest(str(chosen)):
                r = s.use(str(chosen))
                self.assertEqual(r['code'], 'current', r)
                self.assertIsNone(s.public_job())                       # nothing to finish, nothing switched
        if hasattr(os, 'symlink'):
            try:
                (self.tmp / 'link').symlink_to(here.parent, target_is_directory=True)
            except OSError:
                pass                                                    # Windows without the right to make links
            else:
                self.assertEqual(s.use(str(self.tmp / 'link'))['code'], 'current')
        j = s.use(str(usb.parent))
        self.assertEqual((j['state'], j['items'][0]['id']), ('done', 'library'))
        self.assertEqual(s.finish()['folder'], str(usb))
        for _ in range(50):
            if opened:
                break
            time.sleep(0.05)
        self.assertEqual(opened, [str(usb)])                            # the app relaunches into the other library
        self.assertEqual(sorted(p.name for p in here.rglob('*')), ['catalog.json', 'library-data'])   # the open one is untouched

    def test_open_another_library_never_starts_an_install(self):
        """Open another library folder (library=true) takes only a library. A folder of pack zips, or a zip, is refused with
        no job, though first run still uses the same folder's zip (#266, review of #268)."""
        here = self.tmp / 'laptop' / 'LastbookData'
        (here / 'library-data').mkdir(parents=True)
        (here / 'library-data' / 'catalog.json').write_text('{}')
        zips = self.tmp / 'downloads'
        zips.mkdir()
        (zips / 'Lastbook-Small.zip').write_bytes(self.files.data['Lastbook-Small.zip'])
        s = self.setup('manage', current=here)
        for chosen in (zips, zips / 'Lastbook-Small.zip'):
            with self.subTest(chosen.name):
                r = s.use(str(chosen), library_only=True)
                self.assertEqual(r['code'], 'not_library', r)
                self.assertIsNone(s.public_job())
        self.assertEqual(self.files.log, [])
        self.assertEqual(sorted(p.name for p in here.rglob('*')), ['catalog.json', 'library-data'])
        r = api.ROUTES[('POST', 'use')](s, {'path': str(zips), 'library': True})      # the route passes the flag on
        self.assertEqual(r['code'], 'not_library')
        first = self.setup()
        self.assertEqual(first.use(str(zips))['items'][0]['kind'], 'pack')          # first run's "Choose a library zip" as before
        self.wait(first)

    def test_use_zip_is_an_offline_import_in_the_chosen_folder(self):
        """wonderdawg on #465: a library zip already here installs in the folder chosen on screen with nothing downloaded,
        even with the AI ticked on screen and a model server that refuses: the AI, a map or addresses are added after."""
        mine = self.tmp / 'elsewhere' / 'Lastbook-Small.zip'
        mine.parent.mkdir()
        mine.write_bytes(self.zip)
        self.files.data.pop('model.gguf')          # the model download is refused
        chosen = self.tmp / 'chosen'
        s = self.setup()
        j = s.use(str(mine), folder=str(chosen))
        self.assertIsNone(j.get('error'), j)
        self.assertEqual(j['folder'], str(chosen / 'LastbookData'))
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        self.assertEqual(self.wait(s)['state'], 'done')
    def test_restart_mid_download(self):
        """A child process downloads and is killed; a new Setup finds the job paused and resumes with Range."""
        self.files.pace = 0.01
        work = self.parent / 'LastbookData.setup' / 'downloads'
        child = subprocess.Popen([sys.executable, '-B', __file__, '--child', json.dumps(self.child_config())], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            t0 = time.time()
            while time.time() - t0 < 30:
                p = work / 'Lastbook-Small.zip.part'
                if p.exists() and p.stat().st_size > 512 * 1024:
                    break
                time.sleep(0.05)
            else:
                self.fail('the child never started downloading: ' + child.stderr.read1(4000).decode())
        finally:
            child.kill()                      # SIGKILL on a Mac, TerminateProcess on Windows
            child.wait()
            child.stderr.close()              # the pipe is ours to close: left open, it is a ResourceWarning in the output
        had = (work / 'Lastbook-Small.zip.part').stat().st_size
        self.files.pace = 0
        self.files.log.clear()
        s = self.setup()
        j = s.public_job()
        self.assertEqual(j['state'], 'paused')
        self.assertGreaterEqual(j['items'][0]['bytes_done'], had)
        s.resume()
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertTrue(self.files.log[0][1].startswith('bytes='), self.files.log)

    def test_restart_mid_unpack(self):
        """Killed while unpacking: the new run carries on and the result is whole."""
        big = {f'library-data/f{i}.bin': os.urandom(4 * MiB) for i in range(12)}
        big['library-data/catalog.json'] = b'{}'
        zpath = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(zpath, big)
        self.zip = zpath.read_bytes()
        self.files.data['Lastbook-Small.zip'] = self.zip
        cfg = self.child_config()
        cfg['slow_unpack'] = True
        child = subprocess.Popen([sys.executable, '-B', __file__, '--child', json.dumps(cfg)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        unpacked = self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData' / 'library-data'
        try:
            t0 = time.time()
            while time.time() - t0 < 60:
                if unpacked.exists() and len(list(unpacked.glob('f*.bin'))) >= 3:
                    break
                time.sleep(0.02)
            else:
                self.fail('the child never unpacked: ' + child.stderr.read1(4000).decode())
        finally:
            child.kill()                      # SIGKILL on a Mac, TerminateProcess on Windows
            child.wait()
            child.stderr.close()              # the pipe is ours to close: left open, it is a ResourceWarning in the output
        s = self.setup(manifest=self.manifest())
        it = s.public_job()['items'][0]
        self.assertEqual((s.public_job()['state'], it['paused_from'], it['source']), ('paused', 'unpacking', 'download'))
        self.assertEqual(it['bytes_total'], self.installed)
        self.assertGreater(it['bytes_done'], 0)                     # what is unpacked already, not what is downloaded
        s.resume()
        self.assertNotIn('paused_from', s.public_job()['items'][0])
        self.assertEqual(self.wait(s)['state'], 'done')
        lib, _ = api.startup(self.settings)
        for name, data in big.items():
            self.assertEqual(sha((lib / name).read_bytes()), sha(data), name)
        self.assertEqual(list(lib.rglob('*.partial')), [])

    def child_config(self):
        return {'settings': str(self.settings), 'parent': str(self.parent), 'home': str(self.home), 'manifest': self.manifest().current,
                'cert': str(self.tmp_class / 'cert.pem')}


class StaleWorker(Base):
    """A worker that outlives cancel() (which gives up joining) writes nothing: not into the cancelled job, not into the same job
    revived by Retry or Resume, and no new job starts under it."""

    def run_stuck(self, then):
        s = self.setup()
        entered, release = threading.Event(), threading.Event()
        self.late = []          # (thread, what its late write raised) from the stuck worker

        def stuck(self_, it, plan, work, control):
            entered.set()
            release.wait(10)
            try:
                self_._set(it, 'unpacking')
                self_._fail(it, 'network', 'late failure')
                self.late.append((threading.current_thread(), None))
            except Exception as e:
                self.late.append((threading.current_thread(), e))
        with mock.patch.object(api.Setup, '_do_pack', stuck), mock.patch.object(api.Setup, '_join', lambda self_: self_.thread.join(0.2)):
            s.start('small', False)
            self.assertTrue(entered.wait(10))
            first = s.thread
            j = s.cancel()
            then(s)
            release.set()
            first.join(10)
            if s.thread is not first:
                s.thread.join(10)
        return s, j, first

    def late_write(self, first):
        return [e for t, e in self.late if t is first]

    def test_late_worker_does_not_overwrite_a_cancelled_job(self):
        s, j, first = self.run_stuck(lambda s: None)
        self.assertEqual(j['error']['code'], 'cancelled')
        self.assertNotIn('Nothing was changed', j['error']['message'])      # the worker was still running: that would not be true
        self.assertIsInstance(self.late_write(first)[0], download.Stopped)
        after = s.public_job()
        self.assertEqual((after['state'], after['error']['code']), ('failed', 'cancelled'))
        saved = json.loads(api._job_file(self.settings).read_text(encoding='utf-8'))
        self.assertEqual(saved['error']['code'], 'cancelled')

    def test_retry_is_refused_while_the_cancelled_worker_is_still_running(self):
        out = []
        s, j, first = self.run_stuck(lambda s: out.append(s.retry()))
        self.assertEqual(out[0].get('code'), 'busy')
        self.assertIn('still stopping', out[0]['error'])
        self.assertIs(s.thread, first)                                          # no second worker in the same work folder
        self.assertIsInstance(self.late_write(first)[0], download.Stopped)
        self.assertEqual(s.public_job()['error']['code'], 'cancelled')
        self.assertEqual(s.retry()['state'], 'waiting')                         # once it has stopped, Retry runs again
        self.assertEqual(self.wait(s)['state'], 'done')

    def test_resume_is_refused_while_the_cancelled_worker_is_still_running(self):
        out = []

        def then(s):
            s.job['state'], s.job['error'] = 'paused', None
            out.append(s.resume())
        s, j, first = self.run_stuck(then)
        self.assertEqual(out[0].get('code'), 'busy')
        self.assertIs(s.thread, first)
        self.assertIsInstance(self.late_write(first)[0], download.Stopped)
        self.assertEqual(s.public_job()['state'], 'paused')                     # left for the person to resume once it has stopped

    def test_resume_or_retry_from_a_stale_window_gets_the_running_job(self):
        """Codex on #463: another window already restarted the job, so this window's Resume or Retry (from its cached paused or
        failed screen) gets the running job back to show, not "still stopping"; nothing else starts."""
        entered, release, real = threading.Event(), threading.Event(), Path.mkdir

        def mkdir(p, *a, **kw):
            if p.name == 'library-data' and 'unpack' in p.parts and not entered.is_set():
                entered.set()
                release.wait(10)
            return real(p, *a, **kw)
        s = self.setup()
        with mock.patch.object(Path, 'mkdir', mkdir):
            s.start('small', False)
            self.assertTrue(entered.wait(10))
            first = s.thread
            for act in (s.resume, s.retry):
                j = act()
                self.assertNotIn('code', j)
                self.assertIn(j['state'], api.ACTIVE)
                self.assertIs(s.thread, first)                          # no second worker
            release.set()
        self.assertEqual(self.wait(s)['state'], 'done')

    def test_new_job_is_refused_while_the_cancelled_worker_is_still_running(self):
        out = []
        s, j, first = self.run_stuck(lambda s: out.append(s.start('small', False)))
        self.assertEqual(out[0].get('code'), 'busy')
        self.assertIn('still stopping', out[0]['error'])
        self.assertIs(s.thread, first)
        self.assertEqual(s.public_job()['error']['code'], 'cancelled')

    def test_copy_is_refused_while_the_cancelled_worker_is_still_running(self):
        out = []
        s, j, first = self.run_stuck(lambda s: out.append(s.start(copy_to=str(self.tmp / 'usb'))))
        self.assertEqual(out[0].get('code'), 'busy')
        self.assertIn('still stopping', out[0]['error'])
        self.assertIs(s.thread, first)

    def test_late_worker_does_not_install_a_cancelled_model(self):
        """Review of #463: a model copied from another disk is still copying when cancel() gives up the join; once the copy ends
        it does not go into the open library, and the AI is not told about it."""
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data' / 'catalog.json').write_text('{}')
        (cur / 'pack.json').write_text('{"name": "small"}')
        mine = self.tmp / 'usb' / 'model.gguf'
        mine.parent.mkdir()
        mine.write_bytes(self.model)
        s = self.setup('manage', current=cur)
        entered, release, real = threading.Event(), threading.Event(), shutil.copyfile

        def slow_copy(a, b, **kw):
            entered.set()
            release.wait(10)
            return real(a, b, **kw)
        with mock.patch.object(api, '_same_disk', lambda a, b: False), mock.patch.object(api.shutil, 'copyfile', slow_copy), \
                mock.patch.object(api.Setup, '_join', lambda self_: self_.thread.join(0.2)):
            self.assertEqual(s.use(str(mine))['items'][0]['kind'], 'model')
            self.assertTrue(entered.wait(10))
            first = s.thread
            self.assertNotIn('Nothing was changed', s.cancel()['error']['message'])
            self.assertEqual(s.remove_ai().get('code'), 'busy')       # nor is the AI removed under it
            release.set()
            first.join(10)
        self.assertFalse((cur / 'models' / 'model.gguf').exists())
        self.assertEqual(list((cur / 'models').glob('*.partial')), [])
        self.assertEqual(self.marked, [])
        self.assertEqual(s.public_job()['error']['code'], 'cancelled')

    def test_late_progress_does_not_bring_a_cancelled_item_back(self):
        """Review of #463: a progress report from the late worker leaves the cancelled item as it is."""
        s, j, first = self.run_stuck(lambda s: None)
        it = s.job['items'][0]
        stale = []
        t = threading.Thread(target=lambda: (setattr(s._worker, 'job', s.job), setattr(s._worker, 'run', s._run_id - 1),
                                             s._track(it).progress(10, 100, 'unpacking'), stale.append(it['state'])))
        t.start(); t.join(10)
        self.assertEqual(stale, ['failed'])

    def test_a_timed_out_cancel_is_finished_when_the_worker_ends(self):
        """wonderdawg on #463: a real unpack held just before it makes a member's folder outlives the join; Cancel clears the
        staging folder, the worker then writes a .partial there. Retry waits while it is alive, and the partial is gone once it ends."""
        entered, release, real = threading.Event(), threading.Event(), Path.mkdir

        def mkdir(p, *a, **kw):
            if p.name == 'library-data' and 'unpack' in p.parts and not entered.is_set():
                entered.set()
                release.wait(10)
            return real(p, *a, **kw)
        s, seen = self.setup(), []

        def join(self_):      # what another window asks for while cancel() waits on the worker
            seen.append(self_.public_job().get('stopping'))
            self_.thread.join(0.2)
        with mock.patch.object(Path, 'mkdir', mkdir), mock.patch.object(api.Setup, '_join', join):
            s.start('small', False)
            self.assertTrue(entered.wait(10))
            first = s.thread
            j = s.cancel()
            self.assertEqual(seen, [True])                           # stopping from the click, not only once cancel() returns
            self.assertIn('finishing', j['error']['message'])
            self.assertTrue(j.get('stopping'))                       # Setup keeps "Finishing cancellation…" up while it is blocked
            self.assertTrue(s.state()['job'].get('stopping'))
            self.assertEqual(s.retry().get('code'), 'busy')
            release.set()
            first.join(10)
        unpack = self.parent / 'LastbookData.setup' / 'unpack'
        self.assertEqual(sorted(str(p) for p in unpack.rglob('*.partial')) if unpack.exists() else [], [])
        after = s.public_job()
        self.assertEqual((after['state'], after['error']['code']), ('failed', 'cancelled'))
        self.assertIn('Nothing was changed', after['error']['message'])
        self.assertNotIn('stopping', after)                         # and lets it go once the cleanup is done
        saved = json.loads(api._job_file(self.settings).read_text(encoding='utf-8'))
        self.assertIn('Nothing was changed', saved['error']['message'])
        self.assertEqual(s.retry()['state'], 'waiting')          # and Retry runs again
        self.assertEqual(self.wait(s)['state'], 'done')

    def test_cancel_with_nothing_running_says_nothing_changed(self):
        self.files.data.pop('Lastbook-Small.zip')
        s = self.setup()
        with fast_backoff():
            s.start('small', False)
            self.wait(s)
        self.assertIn('Nothing was changed', s.cancel()['error']['message'])


class Switching(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(install, 'RENAME_WAITS', (0,))       # a rename refused for good is not waited on here
        p.start()
        self.addCleanup(p.stop)

    def lib(self, where, marker):
        (where / 'library-data').mkdir(parents=True)
        (where / 'library-data' / 'catalog.json').write_text(marker)
        return where

    def test_upgrade_keeps_old_and_carries_the_model(self):
        cur = self.lib(self.parent / 'LastbookData', 'old')
        (cur / 'models').mkdir()
        (cur / 'models' / 'model.gguf').write_bytes(self.model)
        s = self.setup('manage', current=cur)
        self.assertEqual(s.current_info()['model'], 'gemma')
        s.start('small', True)          # the model is here already: only the pack is fetched
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')   # untouched until the restart
        lib, note = api.startup(self.settings)
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), '{"records": []}')
        self.assertTrue((lib / 'models' / 'model.gguf').is_file())
        old = Path(note['old'])
        self.assertTrue(old.name.startswith('LastbookData.old-'))
        self.assertEqual((old / 'library-data' / 'catalog.json').read_text(), 'old')
        api.opened(self.settings, lib, note)
        s2 = self.setup('manage', current=lib)
        self.assertEqual(s2.current_info()['old'], [str(old)])
        self.assertEqual(s2.delete_old('/etc')['code'], 'unknown')
        s2.delete_old(str(old))
        time.sleep(0.5)
        self.assertFalse(old.exists())

    def test_new_library_that_does_not_open_is_rolled_back(self):
        cur = self.lib(self.parent / 'LastbookData', 'old')
        new = self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')
        install.stage_switch(api._pending_file(self.settings), new, cur)
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(self.parent / 'LastbookData.setup')}))
        real = install.is_library
        calls = []

        def flaky(folder):      # the new one goes missing after the rename: it "does not open"
            calls.append(folder)
            if Path(folder) == cur and len(calls) > 2:
                return False
            return real(folder)
        with mock.patch.object(install, 'is_library', flaky):
            lib, note = api.startup(self.settings)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertIn('error', note)

    def test_failed_switch_leaves_library(self):
        cur = self.lib(self.parent / 'LastbookData', 'old')
        incomplete = self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData'
        incomplete.mkdir(parents=True)
        with self.assertRaises(install.Refused):
            install.switch(incomplete, cur)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ['LastbookData', 'LastbookData.setup'])

    def test_failed_rename_keeps_carried_files(self):
        """#74: when either rename fails, the model and address indexes stay with the library that reopens."""
        real = os.replace
        for failing in ('current aside', 'staged into place'):
            with self.subTest(failing=failing):
                shutil.rmtree(self.parent, ignore_errors=True)
                cur = self.lib(self.parent / 'LastbookData', 'old')
                new = self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')
                (cur / 'models').mkdir()
                (cur / 'models' / 'model.gguf').write_bytes(b'model')
                (cur / 'derived/geo/addresses').mkdir(parents=True)
                (cur / 'derived/geo/addresses/CA.sqlite').write_bytes(b'address index')
                source = cur if failing == 'current aside' else new

                def replace(src, dst):
                    if Path(src) == source:
                        raise PermissionError('in use')
                    return real(src, dst)
                with mock.patch.object(install.os, 'replace', replace), self.assertRaises(install.Refused):
                    install.switch(new, cur)
                self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
                self.assertEqual((cur / 'models' / 'model.gguf').read_bytes(), b'model')
                self.assertEqual((cur / 'derived/geo/addresses/CA.sqlite').read_bytes(), b'address index')
                self.assertFalse((new / 'models' / 'model.gguf').exists())
                self.assertFalse((new / 'derived/geo/addresses/CA.sqlite').exists())

    def carried_libraries(self):
        shutil.rmtree(self.parent, ignore_errors=True)
        cur = self.lib(self.parent / 'LastbookData', 'old')
        new = self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')
        (cur / 'models').mkdir()
        (cur / 'models' / 'model.gguf').write_bytes(b'model')
        (cur / 'derived/geo/addresses').mkdir(parents=True)
        (cur / 'derived/geo/addresses/CA.sqlite').write_bytes(b'address index')
        return cur, new

    def test_failed_carry_puts_back_what_already_moved(self):
        """#83 review: the model moves, the address index does not; both stay with the library that reopens."""
        cur, new = self.carried_libraries()
        real = os.replace

        def replace(src, dst):
            if Path(src).name == 'CA.sqlite':
                raise PermissionError('in use')
            return real(src, dst)
        with mock.patch.object(install.os, 'replace', replace), self.assertRaises(install.Refused) as e:
            install.switch(new, cur)
        self.assertIn('could not move your files', e.exception.message)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertEqual((cur / 'models' / 'model.gguf').read_bytes(), b'model')
        self.assertEqual((cur / 'derived/geo/addresses/CA.sqlite').read_bytes(), b'address index')
        self.assertFalse((new / 'models' / 'model.gguf').exists())

    def test_failed_carry_back_says_where_the_file_is(self):
        """If a moved file cannot go back either, the error names where it is."""
        cur, new = self.carried_libraries()
        real = os.replace

        def replace(src, dst):
            if Path(src).name == 'CA.sqlite' or Path(src) == new / 'models' / 'model.gguf':
                raise PermissionError('in use')
            return real(src, dst)
        with mock.patch.object(install.os, 'replace', replace), self.assertRaises(install.Refused) as e:
            install.switch(new, cur)
        self.assertIn(str(new / 'models' / 'model.gguf'), e.exception.message)
        self.assertEqual((new / 'models' / 'model.gguf').read_bytes(), b'model')
        self.assertEqual((cur / 'derived/geo/addresses/CA.sqlite').read_bytes(), b'address index')

    def test_failed_restore_of_the_old_library_says_where_both_are(self):
        """#83 review: the new library cannot go in place and the old one cannot come back: the error names both folders."""
        cur, new = self.carried_libraries()
        real = os.replace

        def replace(src, dst):
            if Path(dst) == cur:            # any rename into the library's place fails; moving it aside still works
                raise PermissionError('in use')
            return real(src, dst)
        with mock.patch.object(install.os, 'replace', replace), self.assertRaises(install.Refused) as e:
            install.switch(new, cur)
        aside = [p for p in self.parent.iterdir() if p.name.startswith('LastbookData.old-')]
        self.assertEqual(len(aside), 1)
        self.assertIn(str(aside[0]), e.exception.message)
        self.assertIn(str(new / 'models' / 'model.gguf'), e.exception.message)
        self.assertNotIn('moved back', e.exception.message)
        self.assertEqual((aside[0] / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertEqual((new / 'models' / 'model.gguf').read_bytes(), b'model')

    def with_files(self):
        shutil.rmtree(self.parent, ignore_errors=True)
        cur = self.lib(self.parent / 'LastbookData', 'old')
        (cur / 'models').mkdir()
        (cur / 'models' / 'model.gguf').write_bytes(self.model)
        (cur / 'derived' / 'geo' / 'addresses').mkdir(parents=True)
        (cur / 'derived' / 'geo' / 'addresses' / 'NJ.sqlite').write_bytes(b'addr')
        return cur, self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')

    def fail_replace(self, when):
        real = os.replace

        def flaky(a, b, *x, **k):
            if when(Path(a), Path(b)):
                raise OSError('busy')
            return real(a, b, *x, **k)
        return mock.patch.object(os, 'replace', flaky)

    def stranded(self):
        """A failed switch whose model was carried into the staged library and could not go back."""
        cur, new = self.with_files()
        (new / 'models').mkdir()
        os.replace(cur / 'models' / 'model.gguf', new / 'models' / 'model.gguf')
        return cur, new

    def failed_switch_settings(self):
        work = self.parent / 'LastbookData.setup'
        self.settings.mkdir(parents=True, exist_ok=True)
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(work)}))
        return work

    def test_work_folder_is_kept_when_the_switch_failed(self):
        cur, new = self.with_files()
        work = self.failed_switch_settings()
        (work / 'marker').write_text('x')
        jf = api._job_file(self.settings)
        jf.parent.mkdir(parents=True, exist_ok=True)
        jf.write_text(json.dumps({'state': 'done'}))
        api.opened(self.settings, cur, {'target': str(cur), 'old': None, 'error': 'It broke.'})
        self.assertTrue((work / 'marker').exists())
        self.assertFalse(jf.exists())

    def test_a_failed_switch_sets_its_unpacked_library_aside(self):
        cur, new = self.stranded()
        work = self.failed_switch_settings()
        note = {'target': str(cur), 'old': None, 'error': f'Could not move it. The file {new / "models" / "model.gguf"} is still there.'}
        api.opened(self.settings, cur, note)
        self.assertFalse((work / 'unpack').exists())       # the next job unpacks into a fresh folder, not over these files
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertIn(str(kept[0] / 'LastbookData' / 'models' / 'model.gguf'), note['error'])     # the message points at where the files are now

    def test_a_model_a_failed_switch_left_survives_choosing_another_pack(self):
        """#86 empties the unpack folder when another pack comes next. A model a failed switch left there must not go with it."""
        cur, new = self.stranded()
        work = self.failed_switch_settings()
        api.opened(self.settings, cur, {'target': str(cur), 'old': None, 'error': 'Could not move it.'})
        api._fresh_unpack(work / 'unpack', {'pack': 'another', 'revision': 1, 'files': []})
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)

    def test_a_model_survives_another_pack_when_the_unpack_folder_cannot_be_set_aside(self):
        cur, new = self.stranded()
        work = self.failed_switch_settings()
        note = {'target': str(cur), 'old': None, 'error': 'Could not move it.'}
        with self.fail_replace(lambda a, b: a == work / 'unpack'):     # a pack file still open on Windows, say
            api.opened(self.settings, cur, note)
        self.assertFalse((new / 'library-data' / 'catalog.json').exists())     # nothing a next unpack could take as already there
        api._fresh_unpack(work / 'unpack', {'pack': 'another', 'revision': 1, 'files': []})
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertIn(str(kept[0]), note['error'])

    def test_address_indexes_survive_when_the_unpack_folder_cannot_be_set_aside(self):
        """Maintainer on #195: the fallback kept only models, and deleted address indexes the switch had carried."""
        cur, new = self.with_files()
        install.carry_over(cur, new)
        work = self.failed_switch_settings()
        note = {'target': str(cur), 'old': None, 'error': 'Could not move it.'}
        with self.fail_replace(lambda a, b: a == work / 'unpack'):
            api.opened(self.settings, cur, note)
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'LastbookData' / 'derived' / 'geo' / 'addresses' / 'NJ.sqlite').read_bytes(), b'addr')
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertIn(str(kept[0]), note['error'])

    def test_a_model_that_cannot_be_set_aside_is_left_where_it_is(self):
        """Maintainer on #195: when the model could not move either, it was deleted and the message still said it was kept."""
        cur, new = self.with_files()
        install.carry_over(cur, new)
        work = self.failed_switch_settings()
        note = {'target': str(cur), 'old': None, 'error': 'Could not move it.'}
        with self.fail_replace(lambda a, b: a == work / 'unpack' or 'models' in a.parts):
            api.opened(self.settings, cur, note)
        self.assertEqual((new / 'models' / 'model.gguf').read_bytes(), self.model)     # the last copy stays
        self.assertIn(str(new / 'models'), note['error'])                               # and the message says where it is
        self.assertFalse((new / 'library-data' / 'catalog.json').exists())             # the pack's own files still go
        for k in (p for p in work.iterdir() if p.name.startswith('kept-')):
            self.assertFalse((k / 'LastbookData' / 'models').exists())                 # nothing claims it is somewhere else
            self.assertEqual((k / 'LastbookData' / 'derived' / 'geo' / 'addresses' / 'NJ.sqlite').read_bytes(), b'addr')

    def test_a_file_still_open_is_left_and_named_when_the_rest_is_dropped(self):
        """Maintainer on #195: a regular file Windows still has open made the fallback's unlink raise. It stays and is named."""
        cur, new = self.with_files()
        install.carry_over(cur, new)
        work = self.failed_switch_settings()
        (new / 'pack.json').write_text('{}')
        locked = {new / 'pack.json'}
        real = os.unlink

        def unlink(p, *a, **k):
            if Path(p) in locked:
                raise PermissionError(32, 'The process cannot access the file because it is being used by another process', str(p))
            return real(p, *a, **k)
        note = {'target': str(cur), 'old': None, 'error': 'Could not move it.'}
        with self.fail_replace(lambda a, b: a == work / 'unpack' or 'models' in a.parts), mock.patch.object(os, 'unlink', unlink):
            api.opened(self.settings, cur, note)       # does not raise
        self.assertEqual((new / 'models' / 'model.gguf').read_bytes(), self.model)     # the person's files are kept
        for p in locked:
            self.assertTrue(p.exists())
            self.assertIn(str(p), note['error'])        # and the message names what could not be removed

    def test_a_model_left_in_the_unpack_folder_survives_another_pack(self):
        """A model that could not be set aside stays in the unpack folder; the next pack's unpack must not empty it away."""
        cur, new = self.with_files()
        install.carry_over(cur, new)
        work = self.failed_switch_settings()
        note = {'target': str(cur), 'old': None, 'error': 'Could not move it.'}
        with self.fail_replace(lambda a, b: a == work / 'unpack' or 'models' in a.parts):
            api.opened(self.settings, cur, note)
        api._fresh_unpack(work / 'unpack', {'pack': 'another', 'revision': 1, 'files': []})
        kept = [p for p in work.iterdir() if p.name.startswith('kept-') and (p / 'LastbookData' / 'models').exists()]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertEqual([p.name for p in (work / 'unpack').iterdir()], ['unpacked-from.json'])

    def test_a_later_successful_setup_keeps_what_a_failed_switch_set_aside(self):
        cur, new = self.stranded()
        work = self.failed_switch_settings()
        api.opened(self.settings, cur, {'target': str(cur), 'old': None, 'error': 'Could not move it.'})
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')][0]
        (work / 'downloads').mkdir()
        (work / 'downloads' / 'pack.zip').write_text('zip')     # what the next job made
        api.opened(self.settings, cur, {'target': str(cur), 'old': None})
        self.assertTrue((kept / 'LastbookData' / 'models' / 'model.gguf').is_file())   # the message said it was kept there
        self.assertFalse((work / 'downloads').exists())

    def kept_after_failed_switch(self):
        cur, new = self.stranded()
        work = self.failed_switch_settings()
        api.opened(self.settings, cur, {'target': str(cur), 'old': None, 'error': 'Could not move it.'})
        (cur / 'pack.json').write_text('{"name": "old"}')       # so the Small library is downloaded next
        return cur, [p for p in work.iterdir() if p.name.startswith('kept-')][0] / 'LastbookData' / 'models' / 'model.gguf'

    def test_kept_files_survive_another_job_cancelled_part_way(self):
        """wonderdawg on #195: a job started and cancelled while a failed switch's files are kept leaves them."""
        cur, kept = self.kept_after_failed_switch()
        hold = self.files.hold['Lastbook-Small.zip'] = Hold(10)
        s = self.setup('manage', current=cur)
        s.start('small', False)
        self.assertTrue(hold.reached.wait(10))
        self.assertEqual(s.cancel()['error']['code'], 'cancelled')
        hold.release.set()
        self.assertEqual(kept.read_bytes(), self.model)

    def test_kept_files_survive_another_job_and_its_switch(self):
        """...and so does one that finishes, through its switch at the next start and the clean-up after it."""
        cur, kept = self.kept_after_failed_switch()
        s = self.setup('manage', current=cur)
        s.start('small', False)
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(kept.read_bytes(), self.model)
        lib, note = api.startup(self.settings)
        self.assertNotIn('error', note)
        api.opened(self.settings, lib, note)
        self.assertEqual(kept.read_bytes(), self.model)

    def test_switch_that_stopped_after_the_old_library_moved_aside_is_finished_at_next_start(self):
        cur, new = self.with_files()
        pend = api._pending_file(self.settings)
        d = install.stage_switch(pend, new, cur)
        old = install.old_name(cur)
        d['old'] = str(old)
        install._write_json(pend, d)                       # what was written before the rename
        install.carry_over(cur, new)
        os.replace(cur, old)                               # ...and the process stopped here, before the new library went in
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(self.parent / 'LastbookData.setup')}))
        lib, note = api.startup(self.settings)
        self.assertEqual(lib, cur)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertTrue((cur / 'models' / 'model.gguf').is_file())
        self.assertTrue((cur / 'derived' / 'geo' / 'addresses' / 'NJ.sqlite').is_file())
        self.assertEqual(note['old'], str(old))
        self.assertTrue(old.is_dir())

    def stopped_after_the_old_library_moved_aside(self):
        cur, new = self.with_files()
        pend = api._pending_file(self.settings)
        d = install.stage_switch(pend, new, cur)
        old = install.old_name(cur)
        d['old'] = str(old)
        install._write_json(pend, d)                       # what was written before the renames
        install.carry_over(cur, new)
        os.replace(cur, old)
        self.failed_switch_settings()
        return cur, new, pend, old

    def test_switch_that_stopped_after_the_new_library_went_in_is_kept_at_next_start(self):
        """Review of #195 with #313: both renames were made, but the process stopped before the record said 'switched'. The
        next start keeps the switch and the recorded old library: no new name for it, and no retries of a switch already made."""
        cur, new, pend, old = self.stopped_after_the_old_library_moved_aside()
        os.replace(new, cur)                               # ...and the process stopped here, before the record was written
        lib, note = api.startup(self.settings)
        self.assertEqual(lib, cur)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertTrue((cur / 'models' / 'model.gguf').is_file())
        self.assertNotIn('error', note)
        self.assertNotIn('again', note)
        self.assertEqual(note['old'], str(old))
        saved = json.loads(pend.read_text())
        self.assertTrue(saved['switched'])
        self.assertEqual(saved['old'], str(old))           # the recorded old library, not a fresh name
        self.assertNotIn('attempts', saved)
        api.opened(self.settings, lib, note)
        self.assertFalse(pend.exists())
        self.assertEqual(json.loads((self.settings / 'setup.json').read_text())['old'], [str(old)])
        self.assertEqual((old / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertEqual([p for p in self.parent.iterdir() if p.name.startswith('LastbookData.old-')], [old])

    def test_a_missing_staged_library_without_its_recorded_old_one_is_not_taken_as_switched(self):
        """The staged library being gone is not enough: with no recorded old library on disk, nothing says the switch was made."""
        cur, new = self.with_files()
        pend = api._pending_file(self.settings)
        d = install.stage_switch(pend, new, cur)
        d['old'] = str(install.old_name(cur))              # recorded, but never moved there
        install._write_json(pend, d)
        shutil.rmtree(new)
        self.failed_switch_settings()
        lib, note = api.startup(self.settings)
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertIn('error', note)
        self.assertIsNone(note['old'])
        self.assertNotIn('switched', json.loads(pend.read_text()))

    def test_old_library_that_cannot_be_put_back_for_a_moment_is_put_back_at_the_next_start(self):
        """Stopped with the old library aside, and the rename putting it back refused once: the record stays, and the next start
        puts it back and makes the switch."""
        cur, new, pend, old = self.stopped_after_the_old_library_moved_aside()
        with self.fail_replace(lambda a, b: a == old and b == cur):
            lib, note = api.startup(self.settings)
        self.assertIsNone(lib)
        self.assertIn(str(old), note['error'])
        self.assertFalse(cur.exists())
        self.assertEqual((old / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertTrue((old / 'models' / 'model.gguf').is_file())     # carried back before the rename that failed
        self.assertEqual(json.loads(pend.read_text())['old'], str(old))
        lib, note = api.startup(self.settings)
        self.assertEqual(lib, cur)
        self.assertNotIn('error', note)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertTrue((cur / 'models' / 'model.gguf').is_file())
        self.assertTrue((cur / 'derived' / 'geo' / 'addresses' / 'NJ.sqlite').is_file())
        self.assertEqual(note['old'], str(old))
        self.assertEqual((old / 'library-data' / 'catalog.json').read_text(), 'old')

    def test_refused_switch_with_a_typographic_path_on_an_ascii_console_is_kept_for_the_next_start(self):
        """#313 with the say() fix: a refusal naming a file under a folder called with ’ is printed on a console that takes
        only ASCII without raising, so the attempt is still recorded and the switch kept for the next start."""
        self.parent = self.tmp / 'Person’s place'
        cur, new = self.with_files()
        pend = api._pending_file(self.settings)
        install.stage_switch(pend, new, cur)
        self.failed_switch_settings()
        staged_model = new / 'models' / 'model.gguf'
        real = os.replace

        def replace(a, b):
            a = Path(a)
            if a == cur or a == staged_model:     # the library is in use, and so is the model once it has been carried in
                raise PermissionError(13, 'in use')
            return real(a, b)
        console = io.TextIOWrapper(io.BytesIO(), encoding='ascii', errors='strict')
        with mock.patch.object(install.os, 'replace', replace), mock.patch.object(sys, 'stdout', console):
            lib, note = api.startup(self.settings)
            console.flush()
            printed = console.buffer.getvalue().decode('ascii')
        self.assertEqual(lib, cur)
        self.assertTrue(note.get('again'))
        self.assertIn(str(staged_model), note['error'])                 # the person's message keeps the real path
        self.assertIn('setup: switch failed:', printed)
        self.assertIn('Person?s place', printed)
        saved = json.loads(pend.read_text())
        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(staged_model.read_bytes(), self.model)

    def test_a_work_folder_that_is_a_link_is_not_followed(self):
        """Codex on #195: cleaning a work folder that links to the library must not empty the library."""
        cur, new = self.with_files()
        shutil.rmtree(self.parent / 'LastbookData.setup')
        work = self.parent / 'LastbookData.setup'
        work.symlink_to(cur, target_is_directory=True)
        self.failed_switch_settings()
        jf = api._job_file(self.settings)
        jf.parent.mkdir(parents=True, exist_ok=True)
        jf.write_text(json.dumps({'state': 'done'}))
        api.opened(self.settings, cur, None)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertEqual((cur / 'models' / 'model.gguf').read_bytes(), self.model)

    def test_a_stopped_first_install_into_an_empty_folder_keeps_the_new_model(self):
        """Codex on #195: the folder set aside was empty, not a library: nothing was carried, so nothing goes back into it."""
        shutil.rmtree(self.parent, ignore_errors=True)
        cur = self.parent / 'LastbookData'
        cur.mkdir(parents=True)
        new = self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')
        (new / 'models').mkdir()
        (new / 'models' / 'model.gguf').write_bytes(self.model)
        pend = api._pending_file(self.settings)
        d = install.stage_switch(pend, new, cur)
        old = install.old_name(cur)
        d['old'] = str(old)
        install._write_json(pend, d)
        os.replace(cur, old)                               # ...and the process stopped here
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(self.parent / 'LastbookData.setup')}))
        lib, note = api.startup(self.settings)
        self.assertEqual(lib, cur)
        self.assertEqual((cur / 'models' / 'model.gguf').read_bytes(), self.model)

    @unittest.skipUnless(hasattr(install, 'SWITCH_ATTEMPTS'),
                         'needs #313 applied first: a refused switch is retried at the next starts')
    def test_two_refused_starts_keep_the_upgrade_and_the_third_sets_it_aside(self):
        """wonderdawg on #195: apply #313, then this. The first two refused starts leave unpack/ and pending.json,
        so the upgrade can be tried again. The third sets those files aside in work/kept-<time>/, the model included,
        and the error names that folder."""
        cur, new = self.with_files()
        pend = api._pending_file(self.settings)
        install.stage_switch(pend, new, cur)
        work = self.failed_switch_settings()
        staged_model = new / 'models' / 'model.gguf'
        real = os.replace

        def replace(a, b):
            a = Path(a)
            if a == cur or a == staged_model:     # the library is in use, and so is the model once it has been carried in
                raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
            return real(a, b)

        with mock.patch.object(install, 'RENAME_WAITS', (0,), create=True), mock.patch.object(install.os, 'replace', replace):
            for attempt in (1, 2):
                lib, note = api.startup(self.settings)
                self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'old')
                self.assertTrue(note.get('again'))
                self.assertNotIn('kept-', note['error'])
                api.opened(self.settings, lib, note)
                self.assertTrue((work / 'unpack').is_dir())                 # still the staged upgrade, ready to retry
                self.assertTrue(pend.is_file())
                saved = json.loads(pend.read_text())
                self.assertEqual(saved['attempts'], attempt)
                self.assertEqual(saved['new'], str(new))
                self.assertEqual(staged_model.read_bytes(), self.model)     # carried in, and not put back
                self.assertFalse((cur / 'models' / 'model.gguf').exists())
                self.assertFalse(any(p.name.startswith('kept-') for p in work.iterdir()))
            lib, note = api.startup(self.settings)
            self.assertFalse(note.get('again'))
            self.assertFalse(pend.exists())                                 # given up: the record goes
            self.assertEqual(staged_model.read_bytes(), self.model)         # the files are still in unpack until opened()
            api.opened(self.settings, lib, note)
        kept = [p for p in work.iterdir() if p.name.startswith('kept-')]
        self.assertEqual(len(kept), 1)
        self.assertFalse((work / 'unpack').exists())
        self.assertEqual((kept[0] / 'LastbookData' / 'models' / 'model.gguf').read_bytes(), self.model)
        self.assertIn(str(kept[0]), note['error'])                          # the person is told where the files are
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')

    def test_ai_only_in_manage_mode(self):
        cur = self.lib(self.parent / 'LastbookData', '{}')
        (cur / 'pack.json').write_text('{"name": "small"}')
        s = self.setup('manage', current=cur)
        j = s.start('small', True)      # the same library: only the AI
        self.assertEqual([i['kind'] for i in j['items']], ['model'])
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(self.marked, [(str(cur / 'models' / 'model.gguf'), sha(self.model))])
        self.assertEqual(s.start('small', True)['code'], 'nothing')

    def in_use(self, src, times):
        """os.replace refusing to move `src` the first `times` tries, as Windows does while the old app still has files open."""
        real, left = os.replace, [times]

        def replace(a, b):
            if Path(a) == src and left[0]:
                left[0] -= 1
                raise PermissionError(13, 'The process cannot access the file because it is being used by another process')
            return real(a, b)
        return mock.patch.object(install.os, 'replace', replace)

    def test_rename_refused_for_a_moment_is_retried(self):
        """#311: the old app may still be closing; a rename it holds up goes through a moment later."""
        cur = self.lib(self.parent / 'LastbookData', 'old')
        new = self.lib(self.parent / 'LastbookData.setup' / 'unpack' / 'LastbookData', 'new')
        with mock.patch.object(install, 'RENAME_WAITS', (0, 0, 0)), self.in_use(cur, 2):
            old = install.switch(new, cur)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertEqual((old / 'library-data' / 'catalog.json').read_text(), 'old')

    def staged(self):
        cur = self.lib(self.parent / 'LastbookData', 'old')
        work = self.parent / 'LastbookData.setup'
        new = self.lib(work / 'unpack' / 'LastbookData', 'new')
        install.stage_switch(api._pending_file(self.settings), new, cur)
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(work)}))
        api._job_file(self.settings).write_text(json.dumps({'state': 'done'}))
        return cur, new

    def test_refused_switch_is_kept_for_the_next_start(self):
        """#311: a switch refused at the restart is tried again at the next start, and its unpack is kept for it."""
        cur, new = self.staged()
        with self.in_use(cur, 99):
            lib, note = api.startup(self.settings)
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertIn('error', note)
        self.assertEqual(json.loads(api._pending_file(self.settings).read_text())['attempts'], 1)
        api.opened(self.settings, lib, note)
        self.assertTrue(install.is_library(new))
        self.assertTrue(api._pending_file(self.settings).is_file())
        lib, note = api.startup(self.settings)         # the next start: nothing holds the library now
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertNotIn('error', note)
        api.opened(self.settings, lib, note)
        self.assertFalse(api._pending_file(self.settings).exists())
        self.assertFalse(new.exists())

    def test_refused_switch_leaves_setup_to_manage_the_old_library(self):
        """A switch kept for the next start does not leave the finished job behind: setup opens on the library in use, not on
        'Installed', while the unpack and the pending switch stay for the retry."""
        cur, new = self.staged()
        with self.in_use(cur, 99):
            lib, note = api.startup(self.settings)
        api.opened(self.settings, lib, note)
        self.assertFalse(api._job_file(self.settings).exists())
        self.assertTrue(install.is_library(new))
        self.assertTrue(api._pending_file(self.settings).is_file())

    def job_after(self, deferred, installed='small', ai=True):
        cur, new = self.staged()
        (cur / 'pack.json').write_text(json.dumps({'name': installed}))
        if deferred:
            with self.in_use(cur, 99):
                lib, note = api.startup(self.settings)
            api.opened(self.settings, lib, note)
        s = self.setup('manage', current=cur)
        s.start('small', ai)            # with 'small' installed: only the AI; otherwise the Small library is downloaded
        self.assertEqual(self.wait(s)['state'], 'done')
        return cur, new, s

    def test_an_ai_job_keeps_a_deferred_switch_through_the_next_start(self):
        """Adding the AI while a refused switch waits leaves the switch and its unpack: the next start makes it, AI and all."""
        cur, new, s = self.job_after(deferred=True)
        self.assertTrue(api._pending_file(self.settings).is_file())
        self.assertTrue(install.is_library(new))
        lib, note = api.startup(self.settings)
        self.assertNotIn('error', note)
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'new')
        self.assertTrue((lib / 'models' / 'model.gguf').is_file())
        api.opened(self.settings, lib, note)
        self.assertFalse(api._pending_file(self.settings).exists())

    def test_a_library_download_drops_a_deferred_switch(self):
        """A library download rewrites the unpack the refused switch would use: that switch goes, and the new one is staged."""
        cur, new, s = self.job_after(deferred=True, installed='old', ai=False)
        self.assertNotIn('attempts', json.loads(api._pending_file(self.settings).read_text()))

    def test_a_new_job_leaves_a_switch_staged_as_usual(self):
        """A switch staged as usual (not yet tried) is left to the restart."""
        self.job_after(deferred=False)
        self.assertTrue(api._pending_file(self.settings).exists())

    def test_cancelling_an_ai_job_keeps_a_deferred_switch_through_the_next_start(self):
        """wonderdawg on #313: cancelling a job that adds only the AI leaves the refused switch and its unpack for the next start."""
        cur, new = self.staged()
        (cur / 'pack.json').write_text('{"name": "small"}')
        with self.in_use(cur, 99):
            lib, note = api.startup(self.settings)
        api.opened(self.settings, lib, note)
        hold = self.files.hold['model.gguf'] = Hold(10)
        s = self.setup('manage', current=cur)
        s.start('small', True)
        self.assertTrue(hold.reached.wait(10))
        self.assertEqual(s.cancel()['error']['code'], 'cancelled')
        hold.release.set()
        self.assertTrue(api._pending_file(self.settings).is_file())
        self.assertTrue(install.is_library(new))
        lib, note = api.startup(self.settings)
        self.assertNotIn('error', note)
        self.assertEqual((lib / 'library-data' / 'catalog.json').read_text(), 'new')

    def deferred(self):
        cur, new = self.staged()
        with self.in_use(cur, 99):
            lib, note = api.startup(self.settings)
        api.opened(self.settings, lib, note)
        return cur, new

    def test_choosing_another_library_drops_a_deferred_switch(self):
        """Codex on #313: choosing another installed library is not undone at the restart by the refused switch, and the
        staged unpack nothing will use any more goes."""
        cur, new = self.deferred()
        other = self.lib(self.parent / 'Other' / 'LastbookData', 'other')
        s = self.setup('manage', current=cur)
        self.assertEqual(s.use(str(other))['state'], 'done')
        self.assertFalse(api._pending_file(self.settings).exists())
        self.assertFalse((self.parent / 'LastbookData.setup' / 'unpack').exists())
        self.assertEqual(api.startup(self.settings), (None, None))

    def test_choosing_the_switch_target_keeps_a_deferred_switch(self):
        """With another library open, choosing the one the refused switch upgrades keeps the switch and its unpack."""
        cur, new = self.deferred()
        other = self.lib(self.parent / 'Other' / 'LastbookData', 'other')
        s = self.setup('manage', current=other)
        self.assertEqual(s.use(str(cur))['state'], 'done')
        self.assertTrue(api._pending_file(self.settings).is_file())
        self.assertTrue(install.is_library(new))

    def test_retried_switch_carries_the_latest_ai_setting(self):
        """The AI's on/off setting copied at a refused switch is copied again at the retry: what the person set since wins."""
        cur, new = self.staged()
        state = cur / 'derived' / 'llm' / 'state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"enabled": true}')
        with self.in_use(cur, 99):
            lib, note = api.startup(self.settings)
        api.opened(self.settings, lib, note)
        state.write_text('{"enabled": false}')         # changed in the old library, still the one in use
        lib, note = api.startup(self.settings)
        self.assertNotIn('error', note)
        self.assertEqual((lib / 'derived' / 'llm' / 'state.json').read_text(), '{"enabled": false}')

    def test_refused_switch_is_dropped_after_three_starts(self):
        cur, new = self.staged()
        for attempt in (1, 2, 3):
            with self.in_use(cur, 99):
                lib, note = api.startup(self.settings)
            self.assertIn('error', note)
            self.assertEqual(api._pending_file(self.settings).exists(), attempt < 3)
            api.opened(self.settings, lib, note)
        self.assertEqual((cur / 'library-data' / 'catalog.json').read_text(), 'old')
        self.assertFalse(new.exists())          # given up: cleared as before


class PackCompare(unittest.TestCase):
    """Comparing an installed pack-manifest.tsv with a new one (issue #144): read-only, from the two lists alone."""
    HEADER = 'path\tbytes\tsha256\thow\tchanges\n'

    def setUp(self):
        self.tmp = shorttemp.mkdtemp('lastbook-compare-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding='utf-8')
        return p

    def test_compare(self):
        a, b, c = sha(b'a'), sha(b'b'), sha(b'c')
        old = [('pack.json', 10, a), ('library-data/kept.pdf', 100, b), ('library-data/bigger.pdf', 50, c),
               ('library-data/rehashed.pdf', 70, a), ('library-data/gone.pdf', 30, b)]
        new = [('pack.json', 10, a), ('library-data/kept.pdf', 100, b), ('library-data/bigger.pdf', 60, c),
               ('library-data/rehashed.pdf', 70, b), ('library-data/fresh.pdf', 40, c)]
        d = install.compare_pack_manifests(old, new)
        self.assertEqual(d['added'], ['library-data/fresh.pdf'])
        self.assertEqual(d['removed'], ['library-data/gone.pdf'])
        self.assertEqual(d['changed'], ['library-data/bigger.pdf', 'library-data/rehashed.pdf'])     # size, then SHA-256 only
        self.assertEqual(d['unchanged'], ['library-data/kept.pdf', 'pack.json'])
        self.assertEqual(d['unsafe'], [])
        self.assertEqual(d['bytes'], {'added': 40, 'changed': 130, 'unchanged': 110, 'removed': 30})
        self.assertEqual((d['fetch_bytes'], d['reuse_bytes']), (170, 110))
        same = install.compare_pack_manifests(new, new)
        self.assertEqual((same['fetch_bytes'], same['reuse_bytes'], same['changed']), (0, 280, []))

    def test_empty_and_unsafe(self):
        d = install.compare_pack_manifests([], [])
        self.assertEqual((d['added'], d['removed'], d['fetch_bytes'], d['reuse_bytes']), ([], [], 0, 0))
        d = install.compare_pack_manifests([], [('pack.json', 5, sha(b'x'))])
        self.assertEqual((d['added'], d['fetch_bytes']), (['pack.json'], 5))
        d = install.compare_pack_manifests([('../outside', 1, 'x'), ('pack.json', 5, 'y')],
                                           [('/etc/hosts', 9, 'z'), ('../outside', 1, 'x'), ('library-data/../../up', 2, 'w')])
        self.assertEqual(d['unsafe'], ['../outside', '/etc/hosts', 'library-data/../../up'])     # never kept, never fetched
        self.assertEqual((d['removed'], d['unchanged'], d['added'], d['fetch_bytes'], d['reuse_bytes']), (['pack.json'], [], [], 0, 0))

    def test_windows_paths_are_unsafe_on_every_system(self):
        bad = ['\\Windows\\b.dll', '/Windows/d.dll', 'C:c.dll', 'C:\\Windows\\a.dll', '..\\up.txt', 'ok\\fine.pdf', '.']
        d = install.compare_pack_manifests([], [(rel, 1, sha(b'x')) for rel in bad] + [('library-data/ok.pdf', 2, sha(b'y'))])
        self.assertEqual(d['unsafe'], sorted(bad))
        self.assertEqual((d['added'], d['fetch_bytes']), (['library-data/ok.pdf'], 2))

    def test_reading(self):
        p = self.write('m.tsv', self.HEADER + f'pack.json\t5\t{sha(b"x")}\tgenerated\t\n\n  \nlibrary-data/a.pdf\t7\t{sha(b"a")}\r\n')
        self.assertEqual(install.read_pack_manifest(p), [('pack.json', 5, sha(b'x')), ('library-data/a.pdf', 7, sha(b'a'))])
        # A list that lost its header or its rows must not read as "every installed file was removed".
        x = sha(b'x')
        for name, text in (('empty', ''), ('header', self.HEADER), ('nohead', f'pack.json\t5\t{x}\nother.pdf\t5\t{x}\n')):
            with self.subTest(name), self.assertRaises(ValueError):
                install.read_pack_manifest(self.write(name + '.tsv', text))
        for name, row in (('short', 'pack.json\t5\n'), ('size', f'pack.json\tfive\t{x}\n'), ('negative', f'pack.json\t-1\t{x}\n'),
                          ('nopath', f'\t5\t{x}\n'), ('twice', f'pack.json\t5\t{x}\npack.json\t6\t{x}\n'),
                          ('nosha', 'pack.json\t5\t\n'), ('shortsha', 'pack.json\t5\tabc\n'), ('uppersha', f'pack.json\t5\t{x.upper()}\n')):
            with self.subTest(name), self.assertRaises(ValueError):
                install.read_pack_manifest(self.write(name + '.tsv', self.HEADER + row))
        with self.assertRaises(OSError):
            install.read_pack_manifest(self.tmp / 'missing.tsv')

    def test_tool(self):
        """tools/compare_packs.py as a maintainer runs it, as its own process."""
        tool = ROOT / 'tools' / 'compare_packs.py'
        (self.tmp / 'lib').mkdir()
        self.write('lib/pack-manifest.tsv', self.HEADER + f'pack.json\t5\t{sha(b"x")}\t\t\nold.pdf\t3\t{sha(b"o")}\t\t\n')
        new = self.write('new.tsv', self.HEADER + f'pack.json\t5\t{sha(b"x")}\t\t\nfresh.pdf\t2000\t{sha(b"f")}\t\t\n')
        r = subprocess.run([sys.executable, '-B', str(tool), '--data', str(self.tmp / 'lib'), str(new), '--list'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('To fetch: 2.0 KB (added and changed). Already here: 5 bytes.', r.stdout)
        self.assertIn('added:\n  fresh.pdf', r.stdout)
        self.assertIn('removed:\n  old.pdf', r.stdout)
        odd = self.write('odd.tsv', self.HEADER + f'schr\u251c\u2562dingers.pdf\t5\t{sha(b"x")}\t\t\n')
        r = subprocess.run([sys.executable, '-B', str(tool), str(new), str(odd), '--list'], capture_output=True, text=True,
                           env={**os.environ, 'PYTHONIOENCODING': 'cp1252'})
        self.assertEqual(r.returncode, 0, r.stderr)     # a Windows file or pipe cannot hold these letters
        bad = self.write('bad.tsv', self.HEADER + 'pack.json\tfive\tabc\n')
        r = subprocess.run([sys.executable, '-B', str(tool), str(new), str(bad)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('line 2 of', r.stderr)



class PackPlan(unittest.TestCase):
    """An update plan for an installed library (issue #144): read-only, and what isn't the pack's is kept, never removed."""
    HEADER = 'path\tbytes\tsha256\thow\tchanges\n'

    def setUp(self):
        self.tmp = shorttemp.mkdtemp('lastbook-plan-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lib = self.tmp / 'LastbookData'
        self.pack = {'pack.json': b'{"name": "small"}', 'library-data/catalog.json': b'[]', 'library-data/a.pdf': b'a' * 100,
                     'derived/geo/national.sqlite': b'base map' * 10}
        for rel, data in self.pack.items():
            self.put(rel, data)
        self.put('pack-manifest.tsv', self.listing(self.pack).encode())

    def put(self, rel, data):
        f = self.lib / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    def listing(self, files):
        return self.HEADER + ''.join(f'{rel}\t{len(data)}\t{sha(data)}\tclone\t\n' for rel, data in files.items())

    def rows(self, files):
        return [(rel, len(data), sha(data)) for rel, data in files.items()]

    def own_map(self):
        """A state map installed on its own: it replaces the pack's national.sqlite and adds a places index."""
        self.put('derived/geo/national.sqlite', b'state map' * 30)
        self.put('derived/geo/places.sqlite', b'p' * 40)
        files = {'derived/geo/national.sqlite': 270, 'derived/geo/places.sqlite': 40}
        self.put('map-set.json', json.dumps({'name': 'RI', 'files': files}).encode())

    def test_nothing_changed_fetches_nothing(self):
        d = install.plan_update(self.lib, self.rows(self.pack))
        self.assertEqual((d['added'], d['changed'], d['removed'], d['fetch_bytes']), ([], [], [], 0))
        self.assertEqual(d['unchanged'], sorted(self.pack))
        self.assertEqual(d['reuse_bytes'], sum(len(v) for v in self.pack.values()))
        self.assertEqual(d['kept'], {'map': [], 'model': [], 'addresses': [], 'other': []})    # the list itself is not "kept"
        self.assertIsNone(d['map'])

    def test_one_changed_and_one_new(self):
        new = {**self.pack, 'library-data/a.pdf': b'A' * 120, 'library-data/b.pdf': b'b' * 30}
        d = install.plan_update(self.lib, self.rows(new))
        self.assertEqual((d['changed'], d['added'], d['removed']), (['library-data/a.pdf'], ['library-data/b.pdf'], []))
        self.assertEqual((d['bytes']['changed'], d['bytes']['added'], d['fetch_bytes']), (120, 30, 150))

    def test_a_map_installed_on_its_own_is_kept(self):
        self.own_map()
        new = {k: v for k, v in self.pack.items() if k != 'derived/geo/national.sqlite'}   # say the release drops its own map
        for release in (new, {**self.pack, 'derived/geo/national.sqlite': b'new base map' * 9}):
            with self.subTest(len(release)):
                d = install.plan_update(self.lib, self.rows(release))
                every = d['added'] + d['changed'] + d['removed'] + d['unchanged']
                self.assertNotIn('derived/geo/national.sqlite', every)      # neither removed nor fetched over the state map
                self.assertEqual(d['fetch_bytes'], 0)
                self.assertEqual([r for r, _ in d['kept']['map']],
                                 ['derived/geo/national.sqlite', 'derived/geo/places.sqlite', 'map-set.json'])
                self.assertEqual(d['map'], 'RI')

    def test_changed_pack_file_with_a_state_map_installed(self):
        # An older pack with a state map installed on its own; the release changes one pack file and its base map.
        # Only the changed pack file counts toward the fetch; the state map is kept, not fetched over.
        self.own_map()
        release = {**self.pack, 'library-data/a.pdf': b'A' * 120, 'derived/geo/national.sqlite': b'new base map' * 9}
        d = install.plan_update(self.lib, self.rows(release))
        self.assertEqual((d['changed'], d['added'], d['removed']), (['library-data/a.pdf'], [], []))
        self.assertEqual(d['unchanged'], ['library-data/catalog.json', 'pack.json'])
        self.assertEqual((d['fetch_bytes'], d['bytes']['changed']), (120, 120))
        self.assertEqual(d['reuse_bytes'], len(self.pack['library-data/catalog.json']) + len(self.pack['pack.json']))
        self.assertEqual(d['kept']['map'], [('derived/geo/national.sqlite', 270), ('derived/geo/places.sqlite', 40),
                                            ('map-set.json', len((self.lib / 'map-set.json').read_bytes()))])
        self.assertEqual(d['map'], 'RI')

    def test_without_its_own_map_the_packs_map_is_compared(self):
        d = install.plan_update(self.lib, self.rows({**self.pack, 'derived/geo/national.sqlite': b'new base map' * 9}))
        self.assertEqual((d['changed'], d['kept']['map']), (['derived/geo/national.sqlite'], []))

    def test_pack_map_with_a_map_set_is_compared(self):
        map_file = 'derived/geo/national.sqlite'
        pack = {
            **self.pack,
            'pack.json': b'{"name": "small", "map": "us"}',
            'map-set.json': json.dumps({'name': 'us', 'files': {map_file: len(self.pack[map_file])}}).encode(),
        }
        for rel in ('pack.json', 'map-set.json'):
            self.put(rel, pack[rel])
        self.put('pack-manifest.tsv', self.listing(pack).encode())
        replacement = bytes(len(pack[map_file]))

        d = install.plan_update(self.lib, self.rows({**pack, map_file: replacement}))
        self.assertEqual(d['changed'], [map_file])
        self.assertEqual(d['fetch_bytes'], len(replacement))
        self.assertEqual(d['kept']['map'], [])

    def test_model_addresses_and_other_files_are_kept(self):
        self.put('models/model.gguf', b'm' * 50)
        self.put('derived/geo/addresses/RI.sqlite', b'r' * 20)
        self.put('derived/llm/state.json', b'{}')
        self.put('library-data/my-notes.txt', b'mine')
        d = install.plan_update(self.lib, self.rows({k: v for k, v in self.pack.items() if k != 'library-data/a.pdf'}))
        self.assertEqual(d['removed'], ['library-data/a.pdf'])          # only the pack's own file
        self.assertEqual(d['kept']['model'], [('models/model.gguf', 50)])
        self.assertEqual(d['kept']['addresses'], [('derived/geo/addresses/RI.sqlite', 20)])
        self.assertEqual(d['kept']['other'], [('derived/llm/state.json', 2), ('library-data/my-notes.txt', 4)])
        self.assertEqual(d['kept_bytes'], {'map': 0, 'model': 50, 'addresses': 20, 'other': 6})

    def test_nothing_on_disk_changes(self):
        self.own_map()
        self.put('models/model.gguf', b'm' * 50)

        def snapshot():
            return sorted((str(f.relative_to(self.lib)), f.stat().st_size, f.stat().st_mtime_ns, sha(f.read_bytes()))
                          for f in self.lib.rglob('*') if f.is_file())
        before = snapshot()
        install.plan_update(self.lib, self.rows({**self.pack, 'library-data/c.pdf': b'c'}))
        self.assertEqual(snapshot(), before)

    def test_a_broken_installed_list_plans_nothing(self):
        (self.lib / 'pack-manifest.tsv').write_text(self.HEADER, encoding='utf-8')
        with self.assertRaises(ValueError):
            install.plan_update(self.lib, self.rows(self.pack))

    def test_tool(self):
        self.own_map()
        self.put('models/model.gguf', b'm' * 50)
        new = self.tmp / 'new.tsv'
        new.write_text(self.listing({**self.pack, 'library-data/b.pdf': b'b' * 30}), encoding='utf-8')
        r = subprocess.run([sys.executable, '-B', str(ROOT / 'tools' / 'compare_packs.py'), '--data', str(self.lib), str(new), '--list'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Kept as they are, not part of the pack:', r.stdout)
        self.assertIn('your own map (RI)', r.stdout)
        self.assertIn('the local AI', r.stdout)
        self.assertIn('kept (model):\n  models/model.gguf', r.stdout)
        self.assertIn('To fetch: 30 bytes', r.stdout)

class Manage(Base):
    """Repair, removing the AI, and the folder picker's answer."""

    def installed_library(self):
        """A library as setup installs it: the test pack unpacked, with a pack-manifest.tsv of its files, and the model."""
        files = {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small"}', 'library-data/big.bin': os.urandom(MiB)}
        rows = ['path\tbytes\tsha256\thow\tchanges'] + [f'{k}\t{len(v)}\t{sha(v)}\tclone\t' for k, v in files.items()]
        files['pack-manifest.tsv'] = ('\n'.join(rows) + '\n').encode()
        zpath = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(zpath, files)
        self.zip = zpath.read_bytes()
        self.files.data['Lastbook-Small.zip'] = self.zip
        lib = install.unpack(zpath, self.parent, self.installed, Control(), lambda d, t: None)
        (lib / 'models').mkdir()
        (lib / 'models' / 'model.gguf').write_bytes(self.model)
        zpath.unlink()
        return lib

    def test_repair_all_well_fetches_nothing(self):
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        j = s.start(repair=True)
        self.assertEqual([i['kind'] for i in j['items']], ['pack', 'model'])
        self.assertTrue(j['repair'])            # the setup page reads it to say "Checking the library" after a reload
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertTrue(j['repair'])
        self.assertEqual(self.files.log, [])
        self.assertFalse(api._pending_file(self.settings).exists())
        self.assertTrue((lib / 'models' / 'model.gguf').is_file())

    def test_repair_damaged_file_and_model(self):
        lib = self.installed_library()
        for path in (lib / 'library-data' / 'big.bin', lib / 'models' / 'model.gguf'):
            data = bytearray(path.read_bytes())
            data[0] ^= 1
            path.write_bytes(data)
        s = self.setup('manage', current=lib)
        s.start(repair=True)
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(sorted(n for n, _, _ in self.files.log), ['Lastbook-Small.zip', 'model.gguf'])
        new, note = api.startup(self.settings)
        self.assertEqual(sha((new / 'models' / 'model.gguf').read_bytes()), sha(self.model))
        self.assertEqual(s._check_library({'state': '', 'bytes_done': 0}, new, Control()), [])
        self.assertTrue(Path(note['old']).exists())        # the damaged copy is kept aside until deleted

    def test_check_now_downloads_nothing_and_keeps_the_result(self):
        lib = self.installed_library()
        work = self.parent / 'LastbookData.setup'
        shutil.rmtree(work, ignore_errors=True)
        s = self.setup('manage', current=lib)
        self.assertIsNone(s.current_info()['checked'])                  # never checked on this computer
        self.files.log.clear()
        j = s.start(check=True)
        self.assertTrue(j['check'])
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (0, True, False))
        self.assertEqual(self.files.log, [])                             # nothing asked of the server
        self.assertFalse(work.exists())                                  # nothing written beside the library
        self.assertEqual(s.current_info()['checked'], j['result'])
        self.assertEqual(self.setup('manage', current=lib).current_info()['checked'], j['result'])   # kept in the settings folder
        self.assertIsNone(api.startup(self.settings)[1])                 # no switch staged

    def test_check_now_reports_damage_and_changes_nothing(self):
        lib = self.installed_library()
        damaged = []
        for path in (lib / 'library-data' / 'big.bin', lib / 'models' / 'model.gguf'):
            data = bytearray(path.read_bytes())
            data[0] ^= 1
            path.write_bytes(data)
            damaged.append((path, sha(data)))
        s = self.setup('manage', current=lib)
        self.files.log.clear()
        s.start(check=True)
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (2, False, False))
        self.assertEqual(self.files.log, [])                             # a check never downloads, even with damage found
        for path, digest in damaged:
            self.assertEqual(sha(path.read_bytes()), digest)             # and changes nothing: Repair is the person's choice
        s.job = None
        s.start(repair=True)                                             # then Repair fixes it, and the check it ran is kept
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (2, False, True))
        self.assertEqual(sorted(n for n, _, _ in self.files.log), ['Lastbook-Small.zip', 'model.gguf'])
        self.assertEqual(s.current_info()['checked']['repaired'], True)

    def test_check_now_finds_a_model_of_the_wrong_size(self):
        lib = self.installed_library()
        model = lib / 'models' / 'model.gguf'
        model.write_bytes(self.model[:-1])                               # one byte short: current_info() no longer names a model
        s = self.setup('manage', current=lib)
        self.assertIsNone(s.current_info()['model'])
        self.files.log.clear()
        j = s.start(check=True)
        self.assertEqual([i['kind'] for i in j['items']], ['pack', 'model'])
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertIs(j['result']['model_ok'], False)
        self.assertGreaterEqual(j['result']['damaged'], 1)
        self.assertEqual(self.files.log, [])
        self.assertEqual(model.read_bytes(), self.model[:-1])           # the file is unchanged
        s.job = None
        s.start(repair=True)                                             # and Repair, as the check offers, replaces it
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (1, False, True))
        self.assertEqual(sorted(n for n, _, _ in self.files.log), ['model.gguf'])
        self.assertEqual(model.read_bytes(), self.model)

    def test_check_now_without_the_model_or_a_library(self):
        lib = self.installed_library()
        (lib / 'models' / 'model.gguf').unlink()
        s = self.setup('manage', current=lib)
        j = s.start(check=True, ai=True)                                 # the AI is not installed: it is not checked, or fetched
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        j = self.wait(s)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok']), (0, None))
        self.assertEqual(self.setup().start(check=True)['code'], 'nothing')
        self.assertIn('check', self.setup().start(check=True)['error'])

    def drive(self):
        d = self.tmp / 'drive'
        d.mkdir()
        return d

    def test_copy_to_a_drive_holds_the_library_and_nothing_personal(self):
        lib = self.installed_library()
        personal = {'derived/geo/addresses/CA.sqlite': b'addresses', 'derived/index/semantic.sqlite': b'index',
                    'derived/llm/state.json': b'{}', 'notes.txt': b'mine'}
        for rel, data in personal.items():
            (lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (lib / rel).write_bytes(data)
        (lib.parent / 'LastbookMine').mkdir()
        (lib.parent / 'LastbookMine' / 'medication.txt').write_text('private')
        before = {p.relative_to(lib).as_posix(): sha(p.read_bytes()) for p in lib.rglob('*') if p.is_file()}
        s = self.setup('manage', current=lib)
        self.files.log.clear()
        drive = self.drive()
        j = s.start(copy_to=str(drive))
        self.assertTrue(j['copy'])
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        out = drive / 'LastbookData'
        got = sorted(p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file())
        self.assertEqual(got, ['library-data/big.bin', 'library-data/catalog.json', 'pack-manifest.tsv', 'pack.json'])   # the list, and only the list
        for rel in got:
            self.assertEqual((out / rel).read_bytes(), (lib / rel).read_bytes())
        self.assertEqual(self.files.log, [])                                   # nothing downloaded
        self.assertEqual(before, {p.relative_to(lib).as_posix(): sha(p.read_bytes()) for p in lib.rglob('*') if p.is_file()})   # the library untouched
        start = (drive / 'START-HERE.txt').read_text(encoding='utf-8')
        self.assertIn('Small library', start)
        self.assertIn('LastbookData', start)
        self.assertIn('Nothing personal', start)
        self.assertFalse((out / api.COPY_MARK).exists())
        self.assertEqual(j['result']['files'], 4)
        self.assertEqual(sorted(p.name for p in drive.iterdir()), ['LastbookData', 'START-HERE.txt'])   # no work folder, no LastbookMine
        self.assertIsNone(api.startup(self.settings)[1])                       # no switch staged
        self.assertEqual(self.setup('manage', current=lib).job, None)          # a copy is not carried over a restart

    def test_copy_carries_on_and_replaces_a_bad_file(self):
        lib = self.installed_library()
        drive = self.drive()
        out = drive / 'LastbookData'
        (out / 'library-data').mkdir(parents=True)
        (out / api.COPY_MARK).write_text('copying')
        (out / 'library-data' / 'catalog.json').write_bytes((lib / 'library-data' / 'catalog.json').read_bytes())
        good = (out / 'library-data' / 'catalog.json').stat().st_mtime_ns
        bad = bytearray((lib / 'library-data' / 'big.bin').read_bytes())
        bad[5] ^= 1
        (out / 'library-data' / 'big.bin').write_bytes(bad)                    # right size, wrong bytes
        (out / 'library-data' / 'big.bin.partial').write_bytes(b'half')        # an earlier copy stopped here
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((out / 'library-data' / 'big.bin').read_bytes(), (lib / 'library-data' / 'big.bin').read_bytes())
        self.assertEqual((out / 'library-data' / 'catalog.json').stat().st_mtime_ns, good)   # a good file is kept, not copied again
        self.assertFalse((out / 'library-data' / 'big.bin.partial').exists())

    def test_copy_stops_on_a_damaged_library_file(self):
        lib = self.installed_library()
        data = bytearray((lib / 'library-data' / 'big.bin').read_bytes())
        data[0] ^= 1
        (lib / 'library-data' / 'big.bin').write_bytes(data)
        s = self.setup('manage', current=lib)
        drive = self.drive()
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'copy'), j)
        self.assertIn('big.bin does not match its published copy', j['error']['message'])
        self.assertFalse((drive / 'LastbookData' / 'library-data' / 'big.bin').exists())          # the damage is not passed on
        self.assertFalse((drive / 'LastbookData' / 'library-data' / 'big.bin.partial').exists())
        self.assertFalse((drive / 'START-HERE.txt').exists())

    def test_copy_with_a_state_map_takes_the_state_map(self):
        lib = self.installed_library()
        files = {'derived/geo/map.sqlite': b'state map', 'derived/geo/national-status.json': b'{}'}
        for rel, data in files.items():
            (lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (lib / rel).write_bytes(data)
        (lib / 'map-set.json').write_text(json.dumps({'name': 'CA', 'files': {k: len(v) for k, v in files.items()}}))
        s = self.setup('manage', current=lib)
        drive = self.drive()
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        out = drive / 'LastbookData'
        for rel, data in files.items():
            self.assertEqual((out / rel).read_bytes(), data)
        self.assertTrue((out / 'map-set.json').is_file())

    def test_copy_refuses_places_that_would_go_wrong(self):
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        self.assertEqual(s.start(copy_to=str(lib / 'library-data'))['code'], 'folder')            # inside the library
        self.assertEqual(s.start(copy_to=str(lib.parent))['code'], 'folder')                      # onto the library itself
        taken = self.drive() / 'LastbookData'
        taken.mkdir()
        (taken / 'something.txt').write_text('not ours')
        self.assertEqual(s.start(copy_to=str(taken.parent))['code'], 'folder')                    # someone else's folder
        self.assertEqual(s.start(copy_to=str(self.tmp / 'nowhere'))['code'], 'missing')
        with mock.patch.object(hardware, 'disk_of', return_value=(10, 10)):
            other = self.tmp / 'small-drive'
            other.mkdir()
            self.assertEqual(s.start(copy_to=str(other))['code'], 'no_space')
        self.assertEqual(self.setup().start(copy_to=str(self.tmp))['code'], 'nothing')
        self.assertEqual(sorted(p.name for p in taken.iterdir()), ['something.txt'])

    def test_copy_reads_each_file_back_from_the_drive(self):
        """A drive that gives back other bytes than were written (a fake-capacity stick) stops the copy, for listed files and a
        state map's files (which the list has no SHA-256 for) alike, and nothing of that file is left as if whole."""
        lib = self.installed_library()
        files = {'derived/geo/map.sqlite': b'state map'}
        (lib / 'derived' / 'geo').mkdir(parents=True)
        (lib / 'derived' / 'geo' / 'map.sqlite').write_bytes(b'state map')
        (lib / 'map-set.json').write_text(json.dumps({'name': 'CA', 'files': {k: len(v) for k, v in files.items()}}))
        real = api._read_back
        for lying in ('big.bin.partial', 'map.sqlite.partial'):
            with self.subTest(lying=lying):
                drive = self.tmp / ('drive-' + lying)
                drive.mkdir()
                fake = lambda path, control, progress, lying=lying: '0' * 64 if Path(path).name == lying else real(path, control, progress)
                s = self.setup('manage', current=lib)
                with mock.patch.object(api, '_read_back', fake):
                    s.start(copy_to=str(drive))
                    j = self.wait(s)
                self.assertEqual((j['state'], j['error']['code']), ('failed', 'copy'), j)
                self.assertIn('The drive gave back different bytes', j['error']['message'])
                name = lying[:-len('.partial')]
                self.assertFalse(any(p.name in (name, lying) for p in drive.rglob('*')))   # neither the file nor its .partial
                self.assertFalse((drive / 'START-HERE.txt').exists())

    def test_copy_refuses_fat_for_a_file_of_4_gb_before_starting(self):
        lib = self.installed_library()
        rows = (lib / 'pack-manifest.tsv').read_text().rstrip('\n') + f'\nlibrary-data/collections/big.zim\t{5 * 2 ** 30}\t{"0" * 64}\tclone\t\n'
        (lib / 'pack-manifest.tsv').write_text(rows)
        s = self.setup('manage', current=lib)
        drive = self.drive()
        with mock.patch.object(hardware, 'disk_of', return_value=(10 ** 12, 10 ** 12)):
            with mock.patch.object(hardware, 'fs_type', return_value='vfat'):
                r = s.start(copy_to=str(drive))
            self.assertEqual(r['code'], 'fat')
            self.assertIn('exFAT', r['error'])
            self.assertEqual(list(drive.iterdir()), [])                          # refused before anything was written
            with mock.patch.object(hardware, 'fs_type', return_value='exfat'):
                self.assertNotEqual(s.start(copy_to=str(drive)).get('code'), 'fat')
        s.cancel()

    def test_copy_counts_room_in_the_drives_clusters(self):
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        drive = self.drive()
        with mock.patch.object(hardware, 'disk_of', return_value=(2 * 2 ** 30, 10 ** 12)), \
             mock.patch.object(hardware, 'cluster_size', return_value=2 ** 30):        # each file takes a whole cluster
            r = s.start(copy_to=str(drive))
        self.assertEqual(r['code'], 'no_space')
        self.assertGreater(r['need_bytes'], 4 * 2 ** 30)                           # 4 files and 2 folders of 1 GB clusters, not ~1 MB
        with mock.patch.object(hardware, 'cluster_size', return_value=4096):
            s.start(copy_to=str(drive))
            self.assertEqual(self.wait(s)['state'], 'done')

    def test_copy_keeps_someone_elses_start_here(self):
        lib = self.installed_library()
        drive = self.drive()
        (drive / 'START-HERE.txt').write_text('My camping photos are in DCIM.')
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual(j['result']['start_here'], 'START-HERE (Lastbook).txt')
        self.assertEqual((drive / 'START-HERE.txt').read_text(), 'My camping photos are in DCIM.')
        self.assertIn('LastbookData', (drive / 'START-HERE (Lastbook).txt').read_text(encoding='utf-8'))
        s.job = None
        (drive / 'START-HERE.txt').unlink()
        (drive / 'START-HERE (Lastbook).txt').rename(drive / 'START-HERE.txt')     # our own from an earlier copy: replaced
        shutil.rmtree(drive / 'LastbookData')                                      # a finished copy is not copied over: start afresh
        s.start(copy_to=str(drive))
        self.assertEqual(self.wait(s)['result']['start_here'], 'START-HERE.txt')

    def link_or_skip(self, target, link):
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest('cannot make a symbolic link here')

    def test_copy_never_writes_through_a_link_on_the_drive(self):
        """The review case: an unfinished copy whose library-data is a link to someone else's folder. Nothing is written there."""
        lib = self.installed_library()
        drive = self.drive()
        out = drive / 'LastbookData'
        out.mkdir()
        (out / api.COPY_MARK).write_text('copying')
        theirs = self.tmp / 'their-folder'
        theirs.mkdir()
        (theirs / 'catalog.json').write_text('their own file')
        self.link_or_skip(theirs, out / 'library-data')
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'copy'), j)
        self.assertIn('goes through a link', j['error']['message'])
        self.assertEqual((theirs / 'catalog.json').read_text(), 'their own file')
        self.assertEqual(sorted(p.name for p in theirs.iterdir()), ['catalog.json'])             # nothing new written there
        self.assertFalse((drive / 'START-HERE.txt').exists())

    def test_copy_never_writes_through_a_partial_link(self):
        lib = self.installed_library()
        drive = self.drive()
        out = drive / 'LastbookData'
        out.mkdir()
        (out / api.COPY_MARK).write_text('copying')
        theirs = self.tmp / 'their-file.txt'
        theirs.write_text('their own file')
        self.link_or_skip(theirs, out / 'pack.json.partial')
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'copy'), j)
        self.assertEqual(theirs.read_text(), 'their own file')

    def test_copy_refuses_a_lastbookdata_that_is_a_link(self):
        lib = self.installed_library()
        drive = self.drive()
        theirs = self.tmp / 'their-folder'
        theirs.mkdir()
        self.link_or_skip(theirs, drive / 'LastbookData')
        r = self.setup('manage', current=lib).start(copy_to=str(drive))
        self.assertEqual(r['code'], 'folder')
        self.assertEqual(list(theirs.iterdir()), [])

    def test_copy_keeps_both_start_here_names_of_someone_else(self):
        """The review case: someone else's START-HERE.txt and START-HERE (Lastbook).txt both stay as they were."""
        lib = self.installed_library()
        drive = self.drive()
        (drive / 'START-HERE.txt').write_text('first')
        (drive / 'START-HERE (Lastbook).txt').write_text('second')
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(((drive / 'START-HERE.txt').read_text(), (drive / 'START-HERE (Lastbook).txt').read_text()), ('first', 'second'))
        self.assertEqual(j['result']['start_here'], 'START-HERE (Lastbook 2).txt')
        self.assertIn('LastbookData', (drive / 'START-HERE (Lastbook 2).txt').read_text(encoding='utf-8'))
        self.assertEqual(sorted(p.name for p in drive.iterdir() if p.name.endswith('.partial')), [])   # no temporary file left behind

    def test_start_here_is_never_written_through_a_link(self):
        lib = self.installed_library()
        drive = self.drive()
        theirs = self.tmp / 'their-file.txt'
        theirs.write_text('their own file')
        (drive / 'START-HERE.txt').write_text('mine')
        self.link_or_skip(theirs, drive / 'START-HERE (Lastbook).txt')
        s = self.setup('manage', current=lib)
        s.start(copy_to=str(drive))
        j = self.wait(s)
        self.assertEqual(j['result']['start_here'], 'START-HERE (Lastbook 2).txt')
        self.assertEqual(theirs.read_text(), 'their own file')

    def test_copy_refuses_a_link_out_of_the_library(self):
        lib = self.installed_library()
        outside = self.tmp / 'outside-catalog.json'
        outside.write_bytes((lib / 'library-data' / 'catalog.json').read_bytes())    # the same bytes: only the link gives it away
        (lib / 'library-data' / 'catalog.json').unlink()
        try:
            os.symlink(outside, lib / 'library-data' / 'catalog.json')
        except OSError:
            self.skipTest('cannot make a symbolic link here')
        drive = self.drive()
        r = self.setup('manage', current=lib).start(copy_to=str(drive))
        self.assertEqual(r['code'], 'copy')
        self.assertIn('leads outside the library', r['error'])
        self.assertEqual(list(drive.iterdir()), [])

    def test_fs_type_from_mountinfo(self):
        info = self.tmp / 'mountinfo'
        info.write_text('22 1 0:21 / / rw,relatime - btrfs /dev/root rw\n'
                        '30 22 8:17 / /media/you/MY\\040DRIVE rw,nosuid - vfat /dev/sdb1 rw\n'
                        '31 22 8:33 / /media/you/MY rw - exfat /dev/sdc1 rw\n')
        with mock.patch.object(hardware.os.path, 'realpath', side_effect=lambda p: p):
            self.assertEqual(hardware._linux_fs_type('/media/you/MY DRIVE/folder', info), 'vfat')   # the escaped space, longest match
            self.assertEqual(hardware._linux_fs_type('/media/you/MY', info), 'exfat')
            self.assertEqual(hardware._linux_fs_type('/media/you/MYOTHER', info), 'btrfs')           # not inside /media/you/MY
            self.assertEqual(hardware._linux_fs_type('/srv/x', info), 'btrfs')
        self.assertIsNone(hardware._linux_fs_type('/x', self.tmp / 'missing'))

    def test_cancel_a_copy_keeps_the_library_and_a_waiting_switch(self):
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        self.assertEqual(api._pending_file(self.settings).exists(), False)
        s.start(copy_to=str(self.drive()))
        api._pending_file(self.settings).parent.mkdir(parents=True, exist_ok=True)
        api._pending_file(self.settings).write_text('{}')                       # as if an upgrade were waiting for a restart
        j = s.cancel()
        self.assertTrue(api._pending_file(self.settings).exists())            # a copy's cancel never drops it
        self.assertIn(j['state'], ('failed', 'done'))
        if j['state'] == 'failed':
            self.assertEqual(j['error']['code'], 'cancelled')
            self.assertIn('copy again to the same place', j['error']['message'])
        self.assertTrue(api.install.is_library(lib))

    def test_a_late_copy_partial_is_removed_when_the_worker_ends(self):
        """wonderdawg on #463: the real Copy held just before it opens a file's .partial on the drive, Cancel returns, then the
        worker goes on and makes that .partial. It is gone once the worker ends; the file copied before it stays."""
        lib = self.installed_library()
        entered, release, real, opened = threading.Event(), threading.Event(), api._open_new, []

        def open_new(p):
            if str(p).endswith('.partial') and COPY_DRIVE in Path(p).parts:
                opened.append(Path(p))
                if len(opened) == 2 and not entered.is_set():      # the first file is copied, the second is held
                    entered.set()
                    release.wait(10)
            return real(p)
        COPY_DRIVE = 'copy-drive'
        drive = self.parent / COPY_DRIVE
        drive.mkdir()
        s, seen = self.setup('manage', current=lib), []

        def join(self_):
            seen.append(self_.public_job().get('stopping'))
            self_.thread.join(0.2)
        with mock.patch.object(api, '_open_new', open_new), mock.patch.object(api.Setup, '_join', join):
            s.start(copy_to=str(drive))
            self.assertTrue(entered.wait(10))
            first = s.thread
            j = s.cancel()
            self.assertEqual(seen, [True])
            self.assertTrue(j.get('stopping'))                       # Setup shows it is still stopping
            self.assertEqual(s.retry().get('code'), 'busy')
            release.set()
            first.join(10)
            self.assertFalse(first.is_alive())
        out = drive / 'LastbookData'
        self.assertEqual(sorted(str(p) for p in drive.rglob('*.partial')), [])      # the late partial went with the worker
        done = opened[0].with_name(opened[0].name[:-len('.partial')])
        rel = done.relative_to(out).as_posix()
        self.assertEqual(done.read_bytes(), (lib / rel).read_bytes())             # what was already copied stays
        after = s.public_job()
        self.assertEqual((after['state'], after['error']['code']), ('failed', 'cancelled'))
        self.assertNotIn('stopping', after)
        self.assertTrue(api.install.is_library(lib))

    def test_copy_refuses_a_lastbookdata_that_is_a_file(self):
        """A plain file called LastbookData on the drive gets the usual folder refusal, not an internal error."""
        lib = self.installed_library()
        drive = self.drive()
        (drive / 'LastbookData').write_text('not a folder')
        r = self.setup('manage', current=lib).start(copy_to=str(drive))
        self.assertEqual(r['code'], 'folder', r)
        self.assertIn('file called LastbookData', r['error'])
        self.assertEqual((drive / 'LastbookData').read_text(), 'not a folder')

    def test_copy_drive_failure_is_not_the_internet(self):
        """A drive that fails mid-copy (pulled out, an I/O error) stops as 'copy', about the drive: a copy downloads nothing."""
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        real = api._open_new

        def failing(path):
            if str(path).endswith('.partial'):
                raise OSError(errno.EIO, 'Input/output error')
            return real(path)
        with mock.patch.object(api, '_open_new', failing):
            s.start(copy_to=str(self.drive()))
            j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'copy'), j)
        self.assertIn('drive is still connected', j['error']['message'])
        self.assertNotIn('internet', j['error']['message'])

    def test_finish_refuses_a_copy(self):
        """A finished copy has no library to switch to here: finish() says so and switches nothing."""
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        finished = []
        s.on_finish = finished.append
        s.start(copy_to=str(self.drive()))
        self.assertEqual(self.wait(s)['state'], 'done')
        r = s.finish()
        self.assertEqual(r['code'], 'nothing', r)
        time.sleep(0.6)
        self.assertEqual(finished, [])
        self.assertIsNone(api.startup(self.settings)[1])

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_copy_screen_offers_pause_while_copying_and_resume_after(self):
        """waiting -> copying -> paused -> resumed, from the real engine, each drawn by setup.js's own jobControls: Pause stays
        while copying (it used to vanish once the state became 'copying'), Resume replaces it when paused, Cancel is always there."""
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        gate, real = threading.Event(), api._read_back

        def held(path, control, progress):        # the copy waits here, mid-file, until the test lets it go (Pause still works)
            while not gate.wait(0.02):
                control.check()
            return real(path, control, progress)

        def until(state):
            t0 = time.time()
            while s.public_job()['state'] != state:
                self.assertLess(time.time() - t0, 20, s.public_job())
                time.sleep(0.02)
            return s.public_job()
        with mock.patch.object(api, '_read_back', held):
            seen = [s.start(copy_to=str(self.drive())), until('copying'), s.pause(), s.resume(), until('copying')]
            gate.set()
            self.assertEqual(self.wait(s)['state'], 'done')
        self.assertIn(seen[0]['state'], ('waiting', 'copying'))
        self.assertEqual([j['state'] for j in seen[1:]], ['copying', 'paused', 'waiting', 'copying'])
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8'), jobs = JSON.parse(process.argv[2]);
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const ico = () => '';
eval(section('  function jobControls(', '  async function jobAct('));
console.log(JSON.stringify(jobs.map(jobControls)));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js'), json.dumps(seen)],
                           check=True, capture_output=True, text=True, encoding='utf-8')
        for j, html in zip(seen, json.loads(r.stdout)):
            with self.subTest(state=j['state']):
                acts = re.findall(r'data-act="([a-z-]+)"', html)
                self.assertEqual(acts, ['resume', 'cancel'] if j['state'] == 'paused' else ['pause', 'cancel'])

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_copy_paused_at_100_percent_keeps_its_own_words(self):
        """#204 review: the last file's read-back has reported 100% and the copy is paused before it finishes. The page used to say
        "Stopped when Lastbook closed. Resume to finish; nothing needs downloading" with Lastbook open (every paused item looked
        complete). The copy's own line comes first. Then Resume finishes the copy."""
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        gate, reached, real = threading.Event(), threading.Event(), api._read_back

        def held(path, control, progress):         # after the read-back that brings the copy to 100%, wait (Pause still works)
            out = real(path, control, progress)
            it = s.job['items'][0]
            if it['bytes_done'] >= it['bytes_total']:
                reached.set()
                while not gate.wait(0.02):
                    control.check()
            return out
        with mock.patch.object(api, '_read_back', held):
            s.start(copy_to=str(self.drive()))
            self.assertTrue(reached.wait(20))
            paused = s.pause()
            self.assertEqual(paused['state'], 'paused')
            self.assertEqual(paused['items'][0]['bytes_done'], paused['items'][0]['bytes_total'])   # 100%, not finished
            gate.set()
            s.resume()
            self.assertEqual(self.wait(s)['state'], 'done')
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8'), j = JSON.parse(process.argv[2]);
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
eval(section('  const noDownload = ', '  const STOPPED = ') + section('  function jobLead(', '  /* Resume when paused'));   // one eval: noDownload in jobLead's scope
console.log(JSON.stringify({copy: jobLead(j), download: jobLead(Object.assign({}, j, {copy: false}))}));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js'), json.dumps(paused)],
                           check=True, capture_output=True, text=True, encoding='utf-8')
        out = json.loads(r.stdout)
        self.assertEqual(out['copy'], 'What is copied is kept. Resume when you are ready. If Lastbook closes, copy again to the same place to carry on.')
        self.assertNotIn('Stopped when Lastbook closed', out['copy'])
        self.assertIn('Stopped when Lastbook closed', out['download'])   # the same complete item in a download job: unchanged

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_copy_item_line_never_speaks_of_downloads(self):
        """The line under a copy's bar, drawn by setup.js's own fillItem: paused at 100% it said "Stopped. Nothing to download.",
        paused part way it lost "copied and checked", and waiting it said "to download"."""
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const size = b => b + ' B', rate = () => '', eta = () => '', retryIn = () => '', line = {};
const bar = {classList: {contains: () => false}, firstChild: {style: {}}, setAttribute() {}, removeAttribute() {}};
const el = {querySelector: s => s.includes('bar') ? bar : line};
let repair = null, S = {job: {copy: true}};
const checkOnly = () => false, behind = () => false, afterAbove = () => false;
eval(section('  const noDownload = ', '  const above = ') + section('  function fillItem(', '  function jobView(') + 'globalThis.fill = fillItem;');   // one eval: their scope
const show = (state, done) => { fill(el, {kind: 'copy', state, bytes_done: done, bytes_total: 100, source: 'download'}); return line.textContent };
console.log(JSON.stringify({waiting: show('waiting', 0), paused_0: show('paused', 0), paused_40: show('paused', 40), paused_100: show('paused', 100), copying: show('copying', 40)}));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js')], check=True, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(json.loads(r.stdout), {'waiting': '100 B to copy', 'paused_0': '0 B of 100 B copied and checked',
                                                'paused_40': '40 B of 100 B copied and checked', 'paused_100': '100 B of 100 B copied and checked',
                                                'copying': '40 B of 100 B copied and checked'})

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_copy_screen_words(self):
        """A copy's cancel and stops speak of the copy and the drive, never of a download or the internet."""
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const esc = s => String(s == null ? '' : s), ico = () => '', size = String, diskName = () => 'the drive', diskFor = () => null;
const errCode = e => e && e.code, errText = e => e && e.message, cur = () => ({});
let folder = '';
eval(section('  function errorBox(', '  function itemHTML('));
const job = code => ({copy: true, folder: '/srv/x/LastbookData', error: {code, message: 'Lastbook could not read or write a file for the copy. Check the drive is still connected, then copy again to the same place to carry on.'}, items: []});
console.log(JSON.stringify({copy: errorBox(job('copy')), full: errorBox(job('no_space')), cancelled: errorBox(job('cancelled')),
                            confirm: src.includes("j.copy ? 'Stop copying? What is copied stays on the drive.'"),
                            live: src.includes("'The copy was stopped. What was copied is on the drive.'")}));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js')],
                           check=True, capture_output=True, text=True, encoding='utf-8')
        out = json.loads(r.stdout)
        self.assertIn('The copy stopped.', out['copy'])
        self.assertIn('The drive is full.', out['full'])
        self.assertIn('The copy was cancelled.', out['cancelled'])
        for k in ('copy', 'full', 'cancelled'):
            self.assertNotIn('internet', out[k])
            self.assertNotIn('download', out[k].lower())
        self.assertTrue(out['confirm'] and out['live'])

    def test_repair_needs_a_known_pack(self):
        lib = self.parent / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        (lib / 'library-data' / 'catalog.json').write_text('{}')
        self.assertEqual(self.setup('manage', current=lib).start(repair=True)['code'], 'unavailable')
        self.assertEqual(self.setup().start(repair=True)['code'], 'nothing')

    def test_remove_ai(self):
        lib = self.installed_library()
        s = self.setup('manage', current=lib)
        self.assertEqual(s.remove_ai()['freed_bytes'], len(self.model))
        self.assertFalse((lib / 'models' / 'model.gguf').exists())
        self.assertTrue((lib / 'library-data' / 'catalog.json').exists())
        self.assertEqual(s.remove_ai()['code'], 'nothing')
        self.assertEqual(self.setup().remove_ai()['code'], 'nothing')

    def test_file_picker_is_explicit(self):
        picker = mock.Mock(return_value=str(self.tmp / 'model.gguf'))
        s = self.setup(pick_folder=picker)
        self.assertEqual(s.pick(files=True)['path'], str(self.tmp / 'model.gguf'))
        picker.assert_called_once_with(files=True)

    def test_pick_answer(self):
        s = self.setup(pick_folder=lambda: str(self.tmp))
        r = s.pick()
        self.assertEqual(r['path'], str(self.tmp))
        self.assertGreater(r['free_bytes'], 0)
        if sys.platform == 'darwin' and importlib.util.find_spec('Foundation'):   # the disk name needs PyObjC (packaging/requirements_macos.txt)
            self.assertTrue(r['disk'])
        self.assertEqual(self.setup(pick_folder=lambda: None).pick(), {'path': None, 'cancelled': True})
        self.assertTrue(all('name' in d for d in hardware.detect([self.parent])['disks']))

    def damaged_library(self):
        """A library whose big.bin and model are each one bit off: (library, [(path, its damaged sha256)])."""
        lib = self.installed_library()
        damaged = []
        for path in (lib / 'library-data' / 'big.bin', lib / 'models' / 'model.gguf'):
            data = bytearray(path.read_bytes())
            data[0] ^= 1
            path.write_bytes(data)
            damaged.append((path, sha(data)))
        return lib, damaged

    def resumed_check(self, lib, manifest=None):
        """Lastbook reopened on a saved Check now: the job comes back paused, and Resume runs it to its end."""
        kw = {'manifest': manifest} if manifest else {}
        s = self.setup('manage', current=lib, **kw)
        j = s.public_job()
        self.assertEqual((j['state'], j.get('check')), ('paused', True), j)
        self.assertTrue(all(i['state'] == 'paused' and i['bytes_done'] == 0 for i in j['items']), j)   # starts again from the files
        self.files.log.clear()
        s.resume()
        return s, self.wait(s)

    def assert_read_only(self, lib, damaged, j):
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual(self.files.log, [])                             # no request to any server
        for path, digest in damaged:
            self.assertEqual(sha(path.read_bytes()), digest)             # the damaged bytes left as they are
        self.assertFalse(api._work_for(lib).exists())                    # no work or downloads folder
        self.assertIsNone(api.startup(self.settings)[1])                 # nothing staged to switch to

    def test_check_now_saved_before_it_starts_stays_read_only_after_a_restart(self):
        """Saved before its worker started, then Lastbook reopened and Resume pressed: still a check. It used to forget it was one,
        download model.gguf, replace the damaged file and report repaired=true."""
        lib, damaged = self.damaged_library()
        with mock.patch.object(api.Setup, '_run', lambda self_: None):    # the job is saved; the process ends before the worker runs
            self.assertTrue(self.setup('manage', current=lib).start(check=True)['check'])
        s, j = self.resumed_check(lib)
        self.assert_read_only(lib, damaged, j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (2, False, False))
        self.assertEqual(s.current_info()['checked']['damaged'], 2)

    def test_check_now_saved_with_its_items_done_checks_again_after_a_restart(self):
        """Saved after both items finished but before the result was written: the reopened check does not trust the saved
        "done" (it used to report damaged=0 and model_ok=null with the damage still there). It reads the files again."""
        lib, damaged = self.damaged_library()
        with mock.patch.object(api.Setup, '_finish_job', lambda self_, plan, work: None):   # the process ends before the result
            s = self.setup('manage', current=lib)
            s.start(check=True)
            t0 = time.time()
            while not all(i['state'] == 'done' for i in s.public_job()['items']):
                self.assertLess(time.time() - t0, 20)
                time.sleep(0.02)
            s.thread.join(10)
        saved = json.loads(api._job_file(self.settings).read_text(encoding='utf-8'))
        self.assertEqual([i['state'] for i in saved['items']], ['done', 'done'])
        s, j = self.resumed_check(lib)
        self.assert_read_only(lib, damaged, j)
        self.assertEqual((j['result']['damaged'], j['result']['model_ok'], j['result']['repaired']), (2, False, False))

    def test_check_now_keeps_an_earlier_jobs_downloads(self):
        """A finished check is not an install: the next start does not clear the work folder, where a download that stopped
        earlier can still be resumed from."""
        lib = self.installed_library()
        work = api._work_for(lib)
        (work / 'downloads').mkdir(parents=True)
        (work / 'downloads' / 'Lastbook-Small.zip.part').write_bytes(b'x' * 1000)
        self.settings.mkdir(parents=True, exist_ok=True)
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(work)}))
        s = self.setup('manage', current=lib)
        s.start(check=True)
        self.assertEqual(self.wait(s)['state'], 'done')
        api.opened(self.settings, lib, None)                             # what main.py does on the next start
        self.assertTrue((work / 'downloads' / 'Lastbook-Small.zip.part').is_file())
        self.assertEqual(json.loads((self.settings / 'setup.json').read_text())['work'], str(work))

    def test_repair_result_says_what_was_replaced(self):
        """Repair's result tells a whole library downloaded again (the old one kept aside) from the AI file replaced in place."""
        lib = self.installed_library()
        model = lib / 'models' / 'model.gguf'
        model.write_bytes(self.model[:-1])
        s = self.setup('manage', current=lib)
        s.start(repair=True)
        r = self.wait(s)['result']
        self.assertEqual((r['repaired'], r['model_ok'], r['pack_replaced']), (True, False, False))
        big = lib / 'library-data' / 'big.bin'                          # then a library file: the whole pack again
        data = bytearray(big.read_bytes())
        data[0] ^= 1
        big.write_bytes(data)
        s = self.setup('manage', current=lib)
        s.job = None
        s.start(repair=True)
        r = self.wait(s)['result']
        self.assertEqual((r['repaired'], r['pack_replaced']), (True, True))

    def test_cancelling_a_check_keeps_downloads_and_a_staged_update(self):
        """Cancel used the install's path: it deleted the work folder's downloads and dropped a staged update. A check made neither."""
        lib = self.installed_library()
        part = api._work_for(lib) / 'downloads' / 'Lastbook-Medium.zip.part'
        part.parent.mkdir(parents=True)
        part.write_bytes(b'x' * 1000)
        s = self.setup('manage', current=lib)
        reading = threading.Event()

        def held(self_, it, folder, control):
            self_._set(it, 'verifying')
            reading.set()
            while True:
                control.check()
                time.sleep(0.02)
        with mock.patch.object(api.Setup, '_check_library', held):
            s.start(check=True)
            self.assertTrue(reading.wait(20))
            pending = api._pending_file(self.settings)
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text('{}')
            j = s.cancel()
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'cancelled'))
        self.assertEqual(part.read_bytes(), b'x' * 1000)
        self.assertEqual(pending.read_text(), '{}')

    def test_cancelling_a_check_reopened_after_a_restart_keeps_downloads(self):
        lib = self.installed_library()
        part = api._work_for(lib) / 'downloads' / 'Lastbook-Medium.zip.part'
        part.parent.mkdir(parents=True)
        part.write_bytes(b'x' * 1000)
        with mock.patch.object(api.Setup, '_run', lambda self_: None):
            self.setup('manage', current=lib).start(check=True)
        self.assertEqual(self.setup('manage', current=lib).cancel()['error']['code'], 'cancelled')
        self.assertEqual(part.read_bytes(), b'x' * 1000)

    def test_finish_refuses_a_check(self):
        s = self.setup('manage', current=self.installed_library())
        restarted = []
        s.on_finish = restarted.append
        s.start(check=True)
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(s.finish().get('code'), 'nothing')
        time.sleep(0.6)
        self.assertEqual(restarted, [])

    def test_a_read_error_in_a_check_is_not_the_internet(self):
        s = self.setup('manage', current=self.installed_library())
        with mock.patch.object(api.Setup, '_check_library', side_effect=OSError(errno.EIO, 'Input/output error')):
            s.start(check=True)
            j = self.wait(s)
        self.assertEqual((j['state'], j['error']['code']), ('failed', 'check'))
        self.assertNotIn('internet', j['error']['message'])

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_check_and_repair_words(self):
        """setup.js's own words: a check, also one resumed after a restart (j.check, with no memory of having started it), never
        speaks of downloads; Repair's offer and its result tell a whole-library download from an AI file put in place."""
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const errCode = e => e && e.code, noDownload = () => false;
let repair = null;
const jobWords = j => { repair = j.repair && !j.check ? {fixed: false} : null; return {head: jobHead(j), lead: jobLead(j)} };
eval(section('  function jobHead(', '  function jobView('));
eval(section('  function jobLead(', '  /* Resume when paused'));
eval(section('  function repairedWords(', '  /* ---- Check now'));
eval(section('  function repairOffer(', '  function checkedView('));
const esc = String, ico = () => '', itemTitle = it => it.title, WORD = {paused: 'Paused'};
eval(section('  const STEPS = ', '  const WORD = ') + section('  function itemHTML(', '  const itemTitle = '));   // one eval: STEPS in itemHTML's scope
const steps = (j, it) => [...itemHTML(it, j).matchAll(/<li[^>]*>([^<]*)<\/li>/g)].map(m => m[1]);
const job = (state, extra) => Object.assign({state, items: [{state: state === 'paused' ? 'paused' : 'verifying'}], error: null}, extra);
console.log(JSON.stringify({
  checking: jobWords(job('verifying', {check: true, repair: true})),
  paused: jobWords(job('paused', {check: true, repair: true})),
  repairing: jobWords(job('verifying', {repair: true})),
  install: jobWords(job('downloading', {})),
  offer_ai: repairOffer(0, true, false), offer_lib: repairOffer(2, false, false), offer_all: repairOffer(1, true, true),
  did_ai: repairedWords({repaired: true, model_ok: false, damaged: 1, pack_replaced: false}),
  did_lib: repairedWords({repaired: true, model_ok: true, damaged: 1, pack_replaced: true}),
  check_steps: steps({check: true, state: 'paused'}, {kind: 'pack', state: 'paused', bytes_done: 0, bytes_total: 10, title: 'Small'}),
  install_steps: steps({state: 'paused'}, {kind: 'pack', state: 'paused', bytes_done: 0, bytes_total: 10, title: 'Small'}),
}));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js')],
                           check=True, capture_output=True, text=True, encoding='utf-8')
        w = json.loads(r.stdout)
        self.assertEqual(w['checking']['head'], 'Checking the library')
        self.assertIn('downloads nothing', w['checking']['lead'])
        self.assertEqual(w['paused']['head'], 'Paused')
        self.assertIn('Nothing was downloaded or changed', w['paused']['lead'])
        self.assertNotIn('What is downloaded is kept', w['paused']['lead'])
        self.assertEqual(w['repairing']['head'], 'Checking the library')
        self.assertEqual(w['install']['head'], 'Installing')
        self.assertEqual(w['offer_ai'], 'Repair downloads the local AI file again and puts it in place of this one.')
        self.assertNotIn('aside', w['offer_ai'])                          # no damaged model is kept: it is replaced
        self.assertIn('whole library again and keeps the damaged copy aside', w['offer_lib'])
        self.assertEqual(w['offer_all'], 'Repair downloads the whole library again and keeps the damaged copy aside, downloads the local AI '
                                          'file again and puts it in place of this one, and downloads the detailed map again.')
        self.assertIn('put in its place', w['did_ai'])
        self.assertNotIn('kept aside', w['did_ai'])
        self.assertIn('downloaded again', w['did_lib'])
        self.assertIn('kept aside', w['did_lib'])
        self.assertEqual(w['check_steps'], ['Check'])                    # a check's items are only read: no Download or Unpack
        self.assertEqual(w['install_steps'], ['Download', 'Check', 'Unpack', 'Finish'])


def make_map(path, state='CA', extra=None):
    """A state map zip: only map-set files, under LastbookData/, with map-set.json listing them."""
    files = {'derived/geo/map.sqlite': os.urandom(300_000), 'derived/geo/terrain.sqlite': os.urandom(500_000),
             'derived/geo/national-status.json': b'{"state":"complete"}', 'derived/geo/socal-cells.json': b'[]',
             'library-data/geography/resources/atlas.sqlite': os.urandom(100_000)}
    files['map-set.json'] = json.dumps({'name': state, 'files': {k: len(v) for k, v in files.items()}}).encode()
    files.update(extra or {})
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for k, v in files.items():
            z.writestr('LastbookData/' + k, v)
    return files


class WithFlags:
    """A stat result with st_flags, for a system whose own results have none. Everything else is the real result's."""

    def __init__(self, st):
        self._st, self.st_flags = st, 0

    def __getattr__(self, name):
        return getattr(self._st, name)


class CloudOnly(Base):
    """Files of the library whose contents are only in iCloud (#362): found from metadata alone, never opened."""

    def setUp(self):
        super().setUp()
        # These tests tell the code it is on a Mac. Only macOS and the BSDs put st_flags on a stat result, so on Linux and
        # Windows os.lstat is given the flags of an ordinary file here. On a Mac nothing is patched.
        if not hasattr(os.lstat(self.tmp), 'st_flags'):
            real = os.lstat
            patch = mock.patch.object(hardware.os, 'lstat', lambda path, *a, **kw: WithFlags(real(path, *a, **kw)))
            patch.start()
            self.addCleanup(patch.stop)

    def library(self):
        lib = self.tmp / 'LastbookData'
        for rel in ('library-data/catalog.json', 'videos/a.mp4', 'videos/b.mp4', 'zim/old/c.zim'):
            (lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (lib / rel).write_bytes(b'x' * 10)
        return lib

    def placeholders(self, lib, *rels):
        """os.lstat as macOS reports an evicted iCloud item: the SF_DATALESS flag (0x40000000), contents not on this computer."""
        real = os.lstat
        cloud = {str(lib / r) for r in rels}

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            if str(path) not in cloud:
                return st
            return mock.Mock(st_flags=st.st_flags | 0x40000000, st_size=st.st_size, st_mode=st.st_mode)
        return mock.patch.object(hardware.os, 'lstat', lstat)

    def test_placeholder_found_ordinary_file_not(self):
        lib = self.library()
        with mock.patch.object(hardware.sys, 'platform', 'darwin'), self.placeholders(lib, 'videos/b.mp4'):
            r = hardware.cloud_only(lib)
        self.assertEqual(r, {'files': 1, 'folders': 0, 'bytes': 10, 'examples': ['videos/b.mp4']})

    def test_nothing_in_the_cloud(self):
        lib = self.library()
        with mock.patch.object(hardware.sys, 'platform', 'darwin'):
            self.assertEqual(hardware.cloud_only(lib), {'files': 0, 'folders': 0, 'bytes': 0, 'examples': []})

    def test_folder_in_the_cloud_is_not_listed(self):
        # Listing an evicted folder asks iCloud for its contents, which can hang offline: count it, do not open it.
        lib = self.library()
        with mock.patch.object(hardware.sys, 'platform', 'darwin'), self.placeholders(lib, 'zim/old', 'zim/old/c.zim'):
            r = hardware.cloud_only(lib)
        self.assertEqual((r['files'], r['folders'], r['examples']), (0, 1, ['zim/old/']))

    def test_files_are_never_opened(self):
        lib = self.library()
        with mock.patch.object(hardware.sys, 'platform', 'darwin'), self.placeholders(lib, 'videos/a.mp4'), \
                mock.patch('builtins.open', side_effect=AssertionError('opened a file')):
            self.assertEqual(hardware.cloud_only(lib)['files'], 1)

    def test_only_on_a_mac(self):
        with mock.patch.object(hardware.sys, 'platform', 'win32'):
            self.assertIsNone(hardware.cloud_only(self.library()))

    def test_setup_reports_it_for_the_open_library(self):
        lib = self.library()
        s = self.setup('manage', current=lib)
        with mock.patch.object(hardware.sys, 'platform', 'darwin'), self.placeholders(lib, 'videos/a.mp4', 'videos/b.mp4'):
            s.current_info()                       # counting starts in the background, so the page is not held up
            s.cloud_thread.join(10)
            r = s.current_info()['cloud_only']
        self.assertEqual((r['files'], r['examples']), (2, ['videos/a.mp4', 'videos/b.mp4']))

    def info_on(self, system):
        """current_info as the page gets it on this system, and whether a count was started."""
        s = self.setup('manage', current=self.library())
        with mock.patch.object(hardware.sys, 'platform', system):
            info = s.current_info()
        return info, s.cloud_thread

    def test_other_systems_are_not_counting(self):
        # #370 review: None alone also meant "still counting", so Windows and Linux were asked again up to CLOUD_POLLS times.
        for system in ('win32', 'linux'):
            with self.subTest(system=system):
                info, thread = self.info_on(system)
                self.assertEqual((info['cloud_only'], info['cloud_counting'], thread), (None, False, None))
        info, thread = self.info_on('darwin')
        self.assertEqual((info['cloud_only'], info['cloud_counting']), (None, True))
        thread.join(10)

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_page_asks_again_only_while_counting_with_one_timer(self):
        """setup.js's own cloudPoll, fed the engine's answer and drawn five times (every redraw of Manage calls it): no timer on
        Windows or Linux, one timer on a Mac while the count runs, and a new one only after that one has run."""
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8'), infos = JSON.parse(process.argv[2]);
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const out = {};
for (const [system, c] of Object.entries(infos)) {
  const timers = [];
  const setTimeout = f => { timers.push(f); return timers.length };
  let view = 'manage', S = {}, cloudPolls = 0, cloudT = null, cloudFor = '';
  const CLOUD_POLL_MS = 3000, CLOUD_POLLS = 20, call = async () => S, cur = () => c, manageView = () => cloudPoll(c);
  eval(section('  function cloudPoll(', '  function manageView('));
  for (let i = 0; i < 5; i++) cloudPoll(c);
  out[system] = timers.length;
}
console.log(JSON.stringify(out));
"""
        infos = {system: self.info_on(system)[0] for system in ('win32', 'linux', 'darwin')}
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web' / 'setup.js'), json.dumps(infos)],
                           check=True, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(json.loads(r.stdout), {'win32': 0, 'linux': 0, 'darwin': 1})


class Maps(Base):
    def map_manifest(self, extra=None):
        files = make_map(self.tmp / 'Lastbook-Map-CA.zip', extra=extra)
        data = (self.tmp / 'Lastbook-Map-CA.zip').read_bytes()
        self.files.data['Lastbook-Map-CA.zip'] = data
        m = self.manifest().current
        m['maps'] = [{'id': 'CA', 'title': 'California', 'kind': 'state', 'includes': 'x', 'installed_bytes': sum(map(len, files.values())),
                      'download_bytes': len(data), 'available': True,
                      'files': [{'name': 'Lastbook-Map-CA.zip', 'url': f'{self.base}/Lastbook-Map-CA.zip', 'size': len(data), 'sha256': sha(data), 'kind': 'zip'}]}]
        return manifest.Manifest(bundled=m, remote=None, allowed=ALLOWED, context=self.ctx), files

    def test_pack_with_map(self):
        mf, files = self.map_manifest()
        s = self.setup(manifest=mf)
        j = s.start('small', False, map_id='CA')
        self.assertEqual([i['kind'] for i in j['items']], ['pack', 'map'])
        self.assertEqual(self.wait(s)['state'], 'done')
        lib, _ = api.startup(self.settings)
        self.assertEqual(install.map_name(lib), 'CA')
        self.assertEqual((lib / 'derived/geo/map.sqlite').read_bytes(), files['derived/geo/map.sqlite'])   # the state's, not the pack's
        self.assertTrue((lib / 'library-data/catalog.json').is_file())
        self.assertEqual(self.setup('manage', current=lib, manifest=mf).current_info()['map'], 'CA')

    def test_map_only_in_manage_mode(self):
        mf, files = self.map_manifest()
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{}')
        (cur / 'pack.json').write_text('{"name": "small", "map": "base"}')
        (cur / 'derived/geo').mkdir(parents=True)
        for n in ('map.sqlite', 'water.sqlite', 'water.sqlite-wal', 'national.sqlite'):
            (cur / 'derived/geo' / n).write_bytes(b'old ' + n.encode())
        s = self.setup('manage', current=cur, manifest=mf)
        self.assertEqual(s.current_info()['map'], 'base')
        j = s.start(None, False, map_id='CA')
        self.assertEqual([i['kind'] for i in j['items']], ['map'])
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual((cur / 'derived/geo/water.sqlite').read_bytes(), b'old water.sqlite')    # untouched until the restart
        lib, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual(install.map_name(lib), 'CA')
        geo = sorted(p.name for p in (lib / 'derived/geo').iterdir())
        self.assertEqual(geo, ['map.sqlite', 'national-status.json', 'socal-cells.json', 'terrain.sqlite'])   # the old set, -wal too, went
        self.assertEqual((Path(note['map_old']) / 'derived/geo/water.sqlite-wal').read_bytes(), b'old water.sqlite-wal')
        api.opened(self.settings, lib, note)
        self.assertFalse((self.parent / 'LastbookData.setup').exists())
        self.assertEqual(self.setup('manage', current=lib, manifest=mf).start(None, False, map_id='CA')['code'], 'nothing')

    def library_with_state_map(self):
        """#75: a Small library as setup installs it (with its pack-manifest.tsv), then the California map set in place of its own."""
        files = {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small", "map": "base"}',
                 'library-data/big.bin': os.urandom(MiB), 'derived/geo/map.sqlite': b'base map'}
        rows = ['path\tbytes\tsha256\thow\tchanges'] + [f'{k}\t{len(v)}\t{sha(v)}\tclone\t' for k, v in files.items()]
        files['pack-manifest.tsv'] = ('\n'.join(rows) + '\n').encode()
        zpath = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(zpath, files)
        self.zip = zpath.read_bytes()
        self.files.data['Lastbook-Small.zip'] = self.zip
        lib = install.unpack(zpath, self.parent, self.installed, Control(), lambda d, t: None)
        zpath.unlink()
        mf, map_files = self.map_manifest()
        unpacked = install.unpack(self.tmp / 'Lastbook-Map-CA.zip', self.tmp / 'map', None, Control(), lambda d, t: None, allowed=install.map_member)
        install.replace_map_in(lib, unpacked)
        self.assertEqual(install.map_name(lib), 'CA')
        return lib, mf, map_files

    def test_repair_of_another_file_keeps_the_state_map(self):
        lib, mf, map_files = self.library_with_state_map()
        (lib / 'library-data' / 'big.bin').unlink()
        s = self.setup('manage', current=lib, manifest=mf)
        s.start(repair=True)
        self.assertEqual(self.wait(s)['state'], 'done')
        new, _ = api.startup(self.settings)
        self.assertTrue((new / 'library-data' / 'big.bin').is_file())
        self.assertEqual(install.map_name(new), 'CA')
        self.assertEqual((new / 'derived/geo/map.sqlite').read_bytes(), map_files['derived/geo/map.sqlite'])

    def test_repair_fetches_a_missing_state_map_file(self):
        lib, mf, map_files = self.library_with_state_map()
        (lib / 'derived/geo/terrain.sqlite').unlink()
        s = self.setup('manage', current=lib, manifest=mf)
        j = s.start(repair=True)
        self.assertEqual([i['kind'] for i in j['items']], ['pack', 'map'])
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertIn('Lastbook-Map-CA.zip', [n for n, _, _ in self.files.log])
        new, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual(install.map_name(new), 'CA')
        self.assertEqual((new / 'derived/geo/terrain.sqlite').read_bytes(), map_files['derived/geo/terrain.sqlite'])

    def test_check_now_reports_a_damaged_state_map_and_downloads_nothing(self):
        """Check now never downloads, even where Repair would fetch the state map again (#88): it reports the map as damaged."""
        lib, mf, map_files = self.library_with_state_map()
        (lib / 'derived/geo/terrain.sqlite').unlink()
        mf.current['maps'][0]['available'] = False          # not on offer either: a check must still run, not refuse
        s = self.setup('manage', current=lib, manifest=mf)
        self.files.log.clear()
        j = s.start(check=True)
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])       # no map item: nothing to fetch
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['map_ok'], j['result']['damaged'], j['result']['repaired']), (False, 1, False))
        self.assertEqual(self.files.log, [])
        self.assertFalse((lib / 'derived/geo/terrain.sqlite').exists())   # left as it was: Repair is the person's choice
        self.assertEqual((lib / 'derived/geo/map.sqlite').read_bytes(), map_files['derived/geo/map.sqlite'])
        self.assertEqual(s.current_info()['checked']['map_ok'], False)

    def test_check_now_resumed_after_a_restart_checks_the_state_map_again(self):
        """The state map's result is worked out again by the resumed check, from the map's files as they are then."""
        lib, mf, map_files = self.library_with_state_map()
        with mock.patch.object(api.Setup, '_run', lambda self_: None):
            self.setup('manage', current=lib, manifest=mf).start(check=True)
        (lib / 'derived/geo/terrain.sqlite').unlink()                    # damaged while Lastbook was closed
        s = self.setup('manage', current=lib, manifest=mf)
        self.assertEqual((s.public_job()['state'], s.public_job().get('check')), ('paused', True))
        self.files.log.clear()
        s.resume()
        j = self.wait(s)
        self.assertEqual(j['state'], 'done', j)
        self.assertEqual((j['result']['map_ok'], j['result']['damaged'], j['result']['repaired']), (False, 1, False))
        self.assertEqual(self.files.log, [])
        self.assertFalse(api._work_for(lib).exists())

    def test_repair_says_when_a_damaged_state_map_cannot_be_fetched(self):
        lib, mf, _ = self.library_with_state_map()
        (lib / 'derived/geo/terrain.sqlite').unlink()
        mf.current['maps'][0]['available'] = False
        j = self.setup('manage', current=lib, manifest=mf).start(repair=True)
        self.assertEqual(j.get('code'), 'unavailable', j)

    def library_with_own_map(self):
        """#88 review: a library like All, whose pack ships its own map set (the whole US) with map-set.json, as setup installs it."""
        terrain = os.urandom(MiB)
        files = {'library-data/catalog.json': b'{"records": []}', 'pack.json': b'{"name": "small", "map": "us"}',
                 'library-data/big.bin': os.urandom(MiB), 'derived/geo/map.sqlite': b'us map', 'derived/geo/terrain.sqlite': terrain}
        files['map-set.json'] = json.dumps({'name': 'us', 'files': {'derived/geo/map.sqlite': 6, 'derived/geo/terrain.sqlite': MiB}}).encode()
        rows = ['path\tbytes\tsha256\thow\tchanges'] + [f'{k}\t{len(v)}\t{sha(v)}\tclone\t' for k, v in files.items()]
        files['pack-manifest.tsv'] = ('\n'.join(rows) + '\n').encode()
        zpath = self.tmp / 'Lastbook-Small.zip'
        self.installed = make_pack(zpath, files)
        self.zip = zpath.read_bytes()
        self.files.data['Lastbook-Small.zip'] = self.zip
        lib = install.unpack(zpath, self.parent, self.installed, Control(), lambda d, t: None)
        zpath.unlink()
        self.assertEqual(install.map_name(lib), 'us')
        return lib, terrain

    def test_repair_restores_a_file_of_the_packs_own_map(self):
        """All's whole-US map comes with the pack, not as a separate download: Repair checks it and fetches it with the pack."""
        lib, terrain = self.library_with_own_map()
        (lib / 'derived/geo/terrain.sqlite').unlink()
        s = self.setup('manage', current=lib, manifest=self.manifest())
        j = s.start(repair=True)
        self.assertIsNone(j.get('code'), j)
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        self.assertEqual(self.wait(s)['state'], 'done')
        new, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual(install.map_name(new), 'us')
        self.assertEqual((new / 'derived/geo/terrain.sqlite').read_bytes(), terrain)

    def test_repair_restores_a_changed_map_set_json_of_the_packs_own_map(self):
        """#88 review: a changed map-set.json of the pack's own map is a damaged pack file, not a separate map to download."""
        lib, terrain = self.library_with_own_map()
        listed = (lib / 'map-set.json').read_bytes()
        with open(lib / 'map-set.json', 'ab') as f:
            f.write(b'\n')
        (lib / 'derived/geo/terrain.sqlite').unlink()
        s = self.setup('manage', current=lib, manifest=self.manifest())
        j = s.start(repair=True)
        self.assertIsNone(j.get('code'), j)
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        self.assertEqual(self.wait(s)['state'], 'done')
        new, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual((new / 'map-set.json').read_bytes(), listed)
        self.assertEqual((new / 'derived/geo/terrain.sqlite').read_bytes(), terrain)

    def test_repair_keeps_a_state_map_installed_over_the_packs_own(self):
        """A state map chosen on a pack that ships its own map set (as on Medium) is still a separate map: Repair keeps it."""
        lib, _ = self.library_with_own_map()
        mf, map_files = self.map_manifest()
        unpacked = install.unpack(self.tmp / 'Lastbook-Map-CA.zip', self.tmp / 'map', None, Control(), lambda d, t: None, allowed=install.map_member)
        install.replace_map_in(lib, unpacked)
        (lib / 'library-data' / 'big.bin').unlink()
        s = self.setup('manage', current=lib, manifest=mf)
        s.start(repair=True)
        self.assertEqual(self.wait(s)['state'], 'done')
        new, _ = api.startup(self.settings)
        self.assertTrue((new / 'library-data' / 'big.bin').is_file())
        self.assertEqual(install.map_name(new), 'CA')
        self.assertEqual((new / 'derived/geo/map.sqlite').read_bytes(), map_files['derived/geo/map.sqlite'])

    def test_map_into_a_library_with_another_name(self):
        """Windows 1.1.4 test: a library opened with --data from a folder not called LastbookData got the map in a nested
        LastbookData. The job must target the open library as it is."""
        mf, files = self.map_manifest()
        cur = self.parent / 'data'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{}')
        (cur / 'pack.json').write_text('{"name": "small", "map": "base"}')
        s = self.setup('manage', current=cur, manifest=mf)
        j = s.start(None, False, folder=str(cur), map_id='CA')     # as the window sends it: the shown folder, the library itself
        self.assertEqual(j['folder'], str(cur))
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertTrue((self.parent / 'data.setup').is_dir())
        self.assertEqual(api.startup(self.settings, only=self.parent / 'other'), (None, None))   # --data naming another library: not now
        self.assertEqual(install.map_name(cur), 'base')
        lib, note = api.startup(self.settings, only=cur)                                       # --data naming this one: switched
        self.assertEqual((lib, install.map_name(cur)), (cur, 'CA'))
        self.assertFalse((cur / 'LastbookData').exists())
        api.opened(self.settings, lib, note)
        self.assertFalse((self.parent / 'data.setup').exists())

    def test_map_zip_with_other_files_refused(self):
        mf, _ = self.map_manifest(extra={'library-data/catalog.json': b'{}'})
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{"mine": 1}')
        s = self.setup('manage', current=cur, manifest=mf)
        s.start(None, False, map_id='CA')
        j = self.wait(s)
        self.assertEqual(j['error']['code'], 'unpack')
        self.assertEqual((cur / 'library-data/catalog.json').read_text(), '{"mine": 1}')

    def test_bad_new_map_leaves_library(self):
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{}')
        (cur / 'derived/geo').mkdir(parents=True)
        (cur / 'derived/geo/map.sqlite').write_bytes(b'old')
        work = self.parent / 'LastbookData.setup'
        new = work / 'unpack-map' / 'LastbookData'
        (new / 'derived/geo').mkdir(parents=True)
        (new / 'map-set.json').write_text(json.dumps({'name': 'CA', 'files': {'derived/geo/map.sqlite': 999}}))
        (new / 'derived/geo/map.sqlite').write_bytes(b'short')
        self.settings.mkdir(parents=True)
        install.stage_switch(api._pending_file(self.settings), new, cur, kind='map')
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(work)}))
        lib, note = api.startup(self.settings)
        self.assertIn('error', note)
        self.assertEqual((cur / 'derived/geo/map.sqlite').read_bytes(), b'old')

    def test_a_console_that_cannot_show_the_message_still_records_the_error(self):
        """A console in a non-UTF-8 locale, or a legacy Windows code page, cannot print the \u2019 in setup's own
        wording. The diagnostic line must not take the error down with it: the note still carries the message."""
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{}')
        (cur / 'derived/geo').mkdir(parents=True)
        (cur / 'derived/geo/map.sqlite').write_bytes(b'old')
        work = self.parent / 'LastbookData.setup'
        new = work / 'unpack-map' / 'LastbookData'
        (new / 'derived/geo').mkdir(parents=True)
        (new / 'map-set.json').write_text(json.dumps({'name': 'CA', 'files': {'derived/geo/map.sqlite': 999}}))
        (new / 'derived/geo/map.sqlite').write_bytes(b'short')
        self.settings.mkdir(parents=True)
        install.stage_switch(api._pending_file(self.settings), new, cur, kind='map')
        (self.settings / 'setup.json').write_text(json.dumps({'work': str(work)}))
        out = io.TextIOWrapper(io.BytesIO(), encoding='ascii', newline='')
        with mock.patch.object(sys, 'stdout', out):
            lib, note = api.startup(self.settings)
        self.assertIn('\u2019', note['error'])                     # the person's wording is kept whole
        self.assertEqual((cur / 'derived/geo/map.sqlite').read_bytes(), b'old')
        out.flush()
        shown = out.buffer.getvalue().decode('ascii')
        self.assertIn('setup: map switch failed:', shown)       # and the line is still printed, just with ? in it
        self.assertIn('?', shown)

    def test_space_counts_the_map(self):
        mf, _ = self.map_manifest()
        s = self.setup(manifest=mf)
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 ** 9, 10 ** 12)):
            a = s.start('small', False)['need_bytes']
            b = s.start('small', False, map_id='CA')['need_bytes']
        self.assertEqual(b - a, mf.map('CA')['download_bytes'] + mf.map('CA')['installed_bytes'])
        self.assertIn('CA', s.state()['recommend']['map_need_bytes'])
        self.assertIn('maps', s.state()['manifest'])
        self.assertEqual(s.start(None, False, map_id='CA')['code'], 'nothing')    # first run: a library first

    def test_map_zip_without_top_folder(self):
        """The real state zips hold derived/geo/... at the top, with no LastbookData/ folder."""
        flat = self.tmp / 'flat.zip'
        make_map(self.tmp / 'm.zip')
        with zipfile.ZipFile(self.tmp / 'm.zip') as a, zipfile.ZipFile(flat, 'w') as b:
            for i in a.infolist():
                b.writestr(i.filename[len('LastbookData/'):], a.read(i))
        lib = install.unpack(flat, self.tmp / 'out', 10 * MiB, Control(), lambda d, t: None, allowed=install.map_member)
        self.assertEqual(install.map_name(lib), 'CA')
        with zipfile.ZipFile(self.tmp / 'evil.zip', 'w') as z:
            z.writestr('../x.sqlite', b'x')
        with self.assertRaises(install.Refused):
            install.unpack(self.tmp / 'evil.zip', self.tmp / 'out2', 10 * MiB, Control(), lambda d, t: None, allowed=install.map_member)
        flatpack = self.tmp / 'flatpack.zip'
        with zipfile.ZipFile(flatpack, 'w') as z:
            z.writestr('library-data/catalog.json', b'{}')
        with self.assertRaises(install.Refused):     # a pack still needs its LastbookData folder
            install.unpack(flatpack, self.tmp / 'out3', 10 * MiB, Control(), lambda d, t: None)

    def test_pack_map_field(self):
        m = self.manifest().current
        raw = {**m, 'packs': [{**m['packs'][0], 'map': 'CA'}, {**m['packs'][0], 'id': 'x', 'map': 'base'}]}
        v = manifest.validate(raw, ALLOWED)
        self.assertEqual([p['map'] for p in v['packs']], ['CA', None])

    def test_merge_keeps_bundled_maps(self):
        mf, _ = self.map_manifest()
        merged = manifest.merge(mf.current, {**mf.current, 'maps': []})
        self.assertEqual([x['id'] for x in merged['maps']], ['CA'])

    def test_another_maps_unpacked_files_are_not_reused(self):
        """#86 for maps: a file left in unpack-map/ by another map is not taken as whole because its size matches."""
        mf, files = self.map_manifest()
        cur = self.parent / 'LastbookData'
        (cur / 'library-data').mkdir(parents=True)
        (cur / 'library-data/catalog.json').write_text('{}')
        (cur / 'pack.json').write_text('{"name": "small", "map": "base"}')
        stale = self.parent / 'LastbookData.setup' / 'unpack-map' / 'LastbookData' / 'derived/geo/map.sqlite'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'x' * len(files['derived/geo/map.sqlite']))
        s = self.setup('manage', current=cur, manifest=mf)
        s.start(None, False, map_id='CA')
        self.assertEqual(self.wait(s)['state'], 'done')
        lib, note = api.startup(self.settings)
        self.assertIsNone(note.get('error'))
        self.assertEqual((lib / 'derived/geo/map.sqlite').read_bytes(), files['derived/geo/map.sqlite'])


class AddressIndexes(Maps):
    def address_manifest(self, wrong_path=False):
        mf, maps = self.map_manifest()
        name = 'Lastbook-Addresses-CA.zip'
        payload = b'address index fixture' * 1000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            path = 'derived/geo/map.sqlite' if wrong_path else 'derived/geo/addresses/CA.sqlite'
            archive.writestr(path, payload)
        data = buf.getvalue()
        self.files.data[name] = data
        mf.current['address_indexes'] = [{'id': 'CA', 'title': 'California', 'available': True,
            'installed_bytes': len(payload), 'download_bytes': len(data),
            'files': [{'name': name, 'url': f'{self.base}/{name}', 'size': len(data), 'sha256': sha(data), 'kind': 'zip'}]}]
        return mf, payload

    def test_use_zip_of_a_pack_with_a_state_map_downloads_no_addresses(self):
        """Codex on #465: a library zip whose pack names a state map (pack.map) still installs as it is: the address index the
        pack's map would bring is not downloaded with it, so the import finishes offline."""
        mf, _ = self.address_manifest()
        for p in mf.current['packs']:
            if p['id'] == 'small':
                p['map'] = 'CA'
        mine = self.tmp / 'elsewhere' / 'Lastbook-Small.zip'
        mine.parent.mkdir()
        mine.write_bytes(self.files.data['Lastbook-Small.zip'])
        self.files.log.clear()
        s = self.setup(manifest=mf)
        j = s.use(str(mine), folder=str(self.tmp / 'chosen'))
        self.assertIsNone(j.get('error'), j)
        self.assertEqual([i['kind'] for i in j['items']], ['pack'])
        self.assertEqual(self.wait(s)['state'], 'done')
        self.assertEqual(self.files.log, [])                                   # nothing downloaded

    def test_fresh_map_download_includes_addresses(self):
        mf, payload = self.address_manifest()
        setup = self.setup(manifest=mf)
        job = setup.start('small', map_id='CA')
        self.assertEqual([item['kind'] for item in job['items']], ['pack', 'map', 'addresses'])
        self.assertEqual(self.wait(setup)['state'], 'done')
        library, _ = api.startup(self.settings)
        self.assertEqual((library / 'derived/geo/addresses/CA.sqlite').read_bytes(), payload)
        self.assertEqual(self.setup('manage', current=library, manifest=mf).current_info()['address_indexes'], ['CA'])

    def test_first_run_without_a_map_downloads_only_the_pack(self):   # #267: setup's "No local map" sends map and address_id as null
        mf, _ = self.address_manifest()
        setup = self.setup(manifest=mf)
        job = setup.start('small', map_id=None, address_id=None)
        self.assertEqual([item['kind'] for item in job['items']], ['pack'])
        self.assertEqual(self.wait(setup)['state'], 'done')
        library, _ = api.startup(self.settings)
        info = self.setup('manage', current=library, manifest=mf).current_info()
        self.assertEqual((info['map'], info['address_indexes']), ('base', []))

    def test_noted_map_and_address_files_say_what_they_are(self):
        mf, _ = self.address_manifest()
        s = self.setup(manifest=mf)
        for name, kind in (('Lastbook-Map-CA.zip', 'map'), ('Lastbook-Addresses-CA.zip', 'addresses')):
            mine = self.tmp / 'elsewhere' / name
            mine.parent.mkdir(exist_ok=True)
            mine.write_bytes(self.files.data[name])
            self.assertEqual(s.use(str(mine)), {'job': None, 'noted': True, 'kind': kind})

    def test_existing_map_downloads_only_missing_index(self):
        mf, payload = self.address_manifest()
        library = self.parent / 'LastbookData'
        (library / 'library-data').mkdir(parents=True)
        (library / 'library-data/catalog.json').write_text('{}')
        (library / 'pack.json').write_text('{"name":"small","map":"CA"}')
        (library / 'derived/geo').mkdir(parents=True)
        (library / 'derived/geo/map.sqlite').write_bytes(b'existing map')
        setup = self.setup('manage', current=library, manifest=mf)
        job = setup.start(map_id='CA')
        self.assertEqual([item['kind'] for item in job['items']], ['addresses'])
        self.assertIsNotNone(setup._checked_job(json.loads(json.dumps(setup.job))))
        self.assertEqual(self.wait(setup)['state'], 'done')
        self.assertEqual((library / 'derived/geo/map.sqlite').read_bytes(), b'existing map')
        self.assertEqual((library / 'derived/geo/addresses/CA.sqlite').read_bytes(), payload)
        self.assertEqual(setup.start(address_id='CA')['code'], 'nothing')

    def test_another_address_downloads_unpacked_file_is_not_reused(self):
        """#86 for address indexes: a file left in unpack-addresses/ by another download is not taken as whole."""
        mf, payload = self.address_manifest()
        library = self.parent / 'LastbookData'
        (library / 'library-data').mkdir(parents=True)
        (library / 'library-data/catalog.json').write_text('{}')
        (library / 'pack.json').write_text('{"name":"all","map":"us"}')
        stale = self.parent / 'LastbookData.setup' / 'unpack-addresses' / 'LastbookData' / 'derived/geo/addresses/CA.sqlite'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'x' * len(payload))
        setup = self.setup('manage', current=library, manifest=mf)
        setup.start(address_id='CA')
        self.assertEqual(self.wait(setup)['state'], 'done')
        self.assertEqual((library / 'derived/geo/addresses/CA.sqlite').read_bytes(), payload)
        self.assertEqual(list((self.parent / 'LastbookData.setup').glob('kept-*')), [])   # removed, not set aside as the person's files

    def test_all_library_can_add_one_state_addresses(self):
        mf, payload = self.address_manifest()
        library = self.parent / 'LastbookData'
        (library / 'library-data').mkdir(parents=True)
        (library / 'library-data/catalog.json').write_text('{}')
        (library / 'pack.json').write_text('{"name":"all","map":"us"}')
        setup = self.setup('manage', current=library, manifest=mf)
        job = setup.start(address_id='CA')
        self.assertEqual([item['kind'] for item in job['items']], ['addresses'])
        self.assertEqual(self.wait(setup)['state'], 'done')
        self.assertEqual(install.map_name(library), 'us')
        self.assertEqual((library / 'derived/geo/addresses/CA.sqlite').read_bytes(), payload)

    def test_index_cannot_replace_map_files(self):
        mf, _ = self.address_manifest(wrong_path=True)
        setup = self.setup(manifest=mf)
        setup.start('small', address_id='CA')
        self.assertEqual(self.wait(setup)['error']['code'], 'unpack')

    def test_addresses_survive_pack_upgrade(self):
        old = self.tmp / 'old'
        new = self.tmp / 'new'
        (old / 'derived/geo/addresses').mkdir(parents=True)
        source = old / 'derived/geo/addresses/CA.sqlite'
        source.write_bytes(b'address index')
        new.mkdir()
        install.carry_over(old, new)
        self.assertEqual((new / 'derived/geo/addresses/CA.sqlite').read_bytes(), b'address index')
        self.assertFalse(source.exists())

    def test_unavailable_address_index_does_not_block_map(self):
        mf, _ = self.address_manifest()
        mf.current['address_indexes'][0]['available'] = False
        setup = self.setup(manifest=mf)
        job = setup.start('small', map_id='CA')
        self.assertEqual([item['kind'] for item in job['items']], ['pack', 'map'])
        self.assertEqual(self.wait(setup)['state'], 'done')
        library, _ = api.startup(self.settings)
        self.assertEqual(install.map_name(library), 'CA')

    def test_missing_address_index_does_not_block_map(self):
        mf, _ = self.address_manifest()
        mf.current['address_indexes'][0]['id'] = 'RI'
        setup = self.setup(manifest=mf)
        job = setup.start('small', map_id='CA')
        self.assertEqual([item['kind'] for item in job['items']], ['pack', 'map'])
        self.assertEqual(self.wait(setup)['state'], 'done')

    def test_explicit_unavailable_address_index_is_rejected(self):
        mf, _ = self.address_manifest()
        mf.current['address_indexes'][0]['available'] = False
        setup = self.setup(manifest=mf)
        self.assertEqual(setup.start('small', address_id='CA')['code'], 'unavailable')
        self.assertEqual(setup.start('small', address_id='HI')['code'], 'unavailable')

    def test_address_manifest_validation_and_pinning(self):
        mf, _ = self.address_manifest()
        validated = manifest.validate(mf.current, ALLOWED)
        self.assertIsNotNone(validated)
        bad = json.loads(json.dumps(mf.current))
        bad['address_indexes'][0]['id'] = '../CA'
        self.assertIsNone(manifest.validate(bad, ALLOWED))
        fetched = json.loads(json.dumps(validated))
        fetched['address_indexes'][0]['files'][0]['sha256'] = '0' * 64
        merged = manifest.merge(validated, fetched)
        self.assertEqual(merged['address_indexes'][0]['files'], validated['address_indexes'][0]['files'])


class ContentRevisions(Base):
    def revised_manifest(self, revision='2026-11-13-small-v2', actual_revision=None):
        metadata = {'name': 'small', 'map': 'base', 'content_revision': actual_revision or revision}
        path = self.tmp / 'new-small.zip'
        installed = make_pack(path, {'library-data/catalog.json': b'{}',
                                    'pack.json': json.dumps(metadata).encode(),
                                    'derived/geo/map.sqlite': b'base map',
                                    'library-data/new.txt': b'new content'})
        data = path.read_bytes()
        self.files.data['Lastbook-Small.zip'] = data
        mf = self.manifest()
        pack = mf.current['packs'][0]
        pack.update(revision=revision, installed_bytes=installed, download_bytes=len(data))
        pack['files'][0].update(size=len(data), sha256=sha(data))
        return mf

    def old_library(self, revision=''):
        lib = self.parent / 'LastbookData'
        (lib / 'library-data').mkdir(parents=True)
        (lib / 'library-data/catalog.json').write_text('{}')
        (lib / 'pack.json').write_text(json.dumps({'name': 'small', 'content_revision': revision, 'map': 'MI'}))
        geo = lib / 'derived/geo'
        geo.mkdir(parents=True)
        (geo / 'map.sqlite').write_bytes(b'my detailed state map')
        (lib / 'map-set.json').write_text(json.dumps({'name': 'MI', 'files': {'derived/geo/map.sqlite': 21}}))
        (lib / 'models').mkdir()
        (lib / 'models/model.gguf').write_bytes(self.model)
        (geo / 'addresses').mkdir()
        (geo / 'addresses/MI.sqlite').write_bytes(b'my address index')
        (geo / 'addresses/MI.json').write_text('{"files": {}}')
        return lib

    def test_same_pack_refresh_preserves_map_model_and_addresses(self):
        mf = self.revised_manifest()
        library = self.old_library()
        setup = self.setup('manage', current=library, manifest=mf)
        job = setup.start('small', folder=str(library))
        self.assertEqual([item['kind'] for item in job['items']], ['pack'])
        self.assertEqual(setup.job['plan']['pack']['revision'], '2026-11-13-small-v2')
        self.assertEqual(self.wait(setup)['state'], 'done')
        opened, _ = api.startup(self.settings)
        self.assertEqual(opened, library)
        self.assertEqual((library / 'derived/geo/map.sqlite').read_bytes(), b'my detailed state map')
        self.assertEqual(install.map_name(library), 'MI')
        self.assertEqual((library / 'models/model.gguf').read_bytes(), self.model)
        self.assertEqual((library / 'derived/geo/addresses/MI.sqlite').read_bytes(), b'my address index')
        self.assertTrue((library / 'derived/geo/addresses/MI.json').exists())
        refreshed = self.setup('manage', current=library, manifest=mf)
        self.assertEqual(refreshed.current_info()['content_revision'], '2026-11-13-small-v2')
        self.assertEqual(refreshed.start('small', folder=str(library))['code'], 'nothing')

    def test_manifest_revision_validation(self):
        mf = self.revised_manifest()
        self.assertEqual(manifest.validate(mf.current, ALLOWED)['packs'][0]['revision'], '2026-11-13-small-v2')
        mf.current['packs'][0]['revision'] = {'unsafe': 'shape'}
        self.assertIsNone(manifest.validate(mf.current, ALLOWED))

    def test_revised_download_rejects_wrong_content_version(self):
        mf = self.revised_manifest(actual_revision='old')
        library = self.old_library()
        setup = self.setup('manage', current=library, manifest=mf)
        setup.start('small')
        self.assertEqual(self.wait(setup)['error']['code'], 'unpack')
        self.assertEqual((library / 'derived/geo/map.sqlite').read_bytes(), b'my detailed state map')

    def test_legacy_manifest_does_not_refresh_same_pack(self):
        library = self.old_library()
        setup = self.setup('manage', current=library)
        self.assertEqual(setup.start('small', folder=str(library))['code'], 'nothing')


class Recommend(Base):
    def hw(self, ram, free, gpus=(), unified=True):
        return {'ram_bytes': ram, 'gpus': list(gpus), 'unified_memory': unified, 'disks': [{'path': str(self.parent), 'free_bytes': free, 'total_bytes': 10 ** 12}],
                'ai': hardware.estimate_ai(ram, list(gpus), unified, 19_027_384_256)}

    def test_estimates(self):
        self.assertFalse(hardware.estimate_ai(16 * 2 ** 30, [], True, 19_027_384_256)['fits'])
        self.assertTrue(hardware.estimate_ai(24 * 2 ** 30, [], True, 19_027_384_256)['fits'])        # no blanket 32 GB floor
        self.assertFalse(hardware.estimate_ai(16 * 2 ** 30, [], False, 19_027_384_256)['fits'])
        self.assertTrue(hardware.estimate_ai(16 * 2 ** 30, [{'name': 'RTX', 'vram_bytes': 12 * 2 ** 30}], False, 19_027_384_256)['fits'])
        self.assertTrue(hardware.estimate_ai(32 * 2 ** 30, [], True, 19_027_384_256)['estimate'])

    def test_apple_silicon_on_linux(self):
        tree = Path(self.parent) / 'device-tree'
        tree.mkdir(parents=True)
        self.assertIsNone(hardware._apple_silicon_linux(tree))
        (tree / 'compatible').write_bytes(b'apple,j293\0apple,t8103\0apple,arm-platform\0')
        (tree / 'model').write_bytes(b'Apple MacBook Pro (13-inch, M1, 2020)\0')
        self.assertEqual(hardware._apple_silicon_linux(tree), 'Apple MacBook Pro (13-inch, M1, 2020)')
        (tree / 'compatible').write_bytes(b'raspberrypi,5-model-b\0brcm,bcm2712\0')
        self.assertIsNone(hardware._apple_silicon_linux(tree))

    def test_judge_counts_apple_silicon_linux_as_unified(self):
        """On Apple silicon running Linux the graphics share the one memory, as on a Mac: judge() must count the model part
        llama.cpp places on the device against the computer's memory, not only the host part plus the mapped file."""
        import llm_local
        GiB = 2 ** 30
        fit = {'status': 'fits', 'ram_total': 20 * GiB, 'device_bytes': 6 * GiB, 'host_bytes': 11 * GiB,
               'memory': [{'kind': 'device', 'name': 'Apple', 'model': 6 * GiB, 'context': 2 * GiB, 'compute': GiB, 'free': 20 * GiB}]}
        # 6 + 11 GiB fits a 20 GiB unified pool; split, the host must carry 11 + the 10.8 GiB mapped rest, and it does not fit
        detect = getattr(llm_local, '_apple_silicon_linux', None) or (lambda *a, **k: False)
        tree = Path(self.parent) / 'device-tree'
        tree.mkdir(parents=True)
        (tree / 'compatible').write_bytes(b'apple,j293\0apple,arm\0')
        (tree / 'model').write_bytes(b'Apple MacBook Pro\0')
        with mock.patch.object(llm_local.sys, 'platform', 'linux'), \
             mock.patch('platform.machine', return_value='aarch64'), \
             mock.patch.object(llm_local, '_apple_silicon_linux', lambda: detect(str(tree)), create=True):
            self.assertTrue(llm_local.judge(dict(fit))['fits'])
        (tree / 'compatible').unlink()
        with mock.patch.object(llm_local.sys, 'platform', 'linux'), \
             mock.patch('platform.machine', return_value='aarch64'), \
             mock.patch.object(llm_local, '_apple_silicon_linux', lambda: detect(str(tree)), create=True):
            self.assertFalse(llm_local.judge(dict(fit))['fits'])          # no device tree, so not unified

    def test_apple_silicon_linux_shares_the_setup_check(self):
        """The runner's check delegates to engine/setup/hardware.py's own device-tree reader, with a local fallback if the
        engine cannot be imported."""
        import llm_local
        import platform as platform_mod
        tree = Path(self.parent) / 'device-tree-share'
        tree.mkdir(parents=True)
        (tree / 'compatible').write_bytes(b'apple,t8103\0apple,arm\0')
        (tree / 'model').write_bytes(b'Apple MacBook Pro\0')
        with mock.patch('platform.machine', return_value='aarch64'):
            self.assertTrue(llm_local._apple_silicon_linux(str(tree)))
        (tree / 'compatible').write_bytes(b'raspberrypi,5-model-b\0broadcom,bcm2712\0')
        with mock.patch('platform.machine', return_value='aarch64'):
            self.assertFalse(llm_local._apple_silicon_linux(str(tree)))
        with mock.patch('platform.machine', return_value='x86_64'):
            self.assertFalse(llm_local._apple_silicon_linux(str(tree)))
        (tree / 'compatible').unlink()

    def test_pack_and_ai_by_space(self):
        s = self.setup()
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 ** 12, 2 * 10 ** 12)):
            r = s.recommend(self.hw(64 * 2 ** 30, 10 ** 12))
        self.assertEqual((r['pack'], r['ai']), ('small', True))
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 ** 8, 10 ** 11)):
            r = s.recommend(self.hw(64 * 2 ** 30, 10 ** 8))
        self.assertIsNone(r['pack'])
        self.assertIn('free', r['why'])
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 ** 12, 2 * 10 ** 12)):
            r = s.recommend(self.hw(8 * 2 ** 30, 10 ** 12))
        self.assertEqual((r['pack'], r['ai']), ('small', False))

    def test_home_disk_first(self):
        """Fresh-user test, 12 Nov: an 8 TB home disk with 137 GB free and an external drive with 605 GB. The library stays on the home disk."""
        m = self.manifest().current
        medium = {**m['packs'][0], 'id': 'medium', 'title': 'Medium', 'installed_bytes': 67 * 10 ** 9, 'download_bytes': 67 * 10 ** 9,
                  'files': [{**m['packs'][0]['files'][0], 'name': 'Lastbook-Medium.zip', 'size': 67 * 10 ** 9}]}
        small = {**m['packs'][0], 'installed_bytes': 8 * 10 ** 9, 'download_bytes': 8 * 10 ** 9,
                 'files': [{**m['packs'][0]['files'][0], 'size': 8 * 10 ** 9}]}
        all_pack = {**medium, 'id': 'all', 'title': 'All', 'installed_bytes': 400 * 10 ** 9}
        s = self.setup(manifest=manifest.Manifest(bundled={**m, 'packs': [small, medium, all_pack]}, remote=None))
        with mock.patch.object(hardware, 'disk_of', return_value=(10 ** 12, 2 * 10 ** 12)):
            self.assertEqual(s.recommend(self.hw(512 * 2 ** 30, 10 ** 12))['pack'], 'small')
            low_ram = s.recommend(self.hw(8 * 2 ** 30, 10 ** 12))
            self.assertEqual((low_ram['pack'], low_ram['ai']), ('small', False))
        ext = {'path': '/Volumes/Ext', 'name': 'Ext', 'free_bytes': 605 * 10 ** 9, 'total_bytes': 10 ** 12}
        hw = {**self.hw(512 * 2 ** 30, 137 * 10 ** 9), 'disks': [{'path': str(self.home), 'free_bytes': 137 * 10 ** 9, 'total_bytes': 8 * 10 ** 12}, ext]}
        same = lambda a, b: not str(a).startswith('/Volumes/')
        with mock.patch.object(hardware, 'disk_of', lambda p: (137 * 10 ** 9, 8 * 10 ** 12)), mock.patch.object(api, '_same_disk', same):
            r = s.recommend(hw)
            self.assertEqual((r['pack'], r['folder']), ('small', str(self.parent)))
            self.assertTrue(r['ai'])
            self.assertIn('Small is selected', r['why'])
        with mock.patch.object(hardware, 'disk_of', lambda p: (10 * 10 ** 9, 8 * 10 ** 12)), mock.patch.object(api, '_same_disk', same):
            r = s.recommend({**hw, 'disks': [{'path': str(self.home), 'free_bytes': 10 ** 10, 'total_bytes': 8 * 10 ** 12}, ext]})
            self.assertEqual((r['pack'], r['folder']), ('small', str(Path('/Volumes/Ext'))))
            self.assertIn('Keep that disk connected', r['why'])

    def test_disk_detection_never_scans_unselected_drives(self):
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            self.assertFalse(str(path).startswith('/Volumes'), str(path))
            return original_stat(path, *args, **kwargs)

        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform), \
                    mock.patch.object(sys, 'platform', platform), \
                    mock.patch.object(Path, 'stat', stat), \
                    mock.patch.object(Path, 'iterdir', side_effect=AssertionError('Automatic directory scan')), \
                    mock.patch.object(Path, 'home', side_effect=AssertionError('Automatic home probe')), \
                    mock.patch.object(hardware, 'disk_name', return_value='Selected disk'):
                disks = hardware.candidate_disks([self.parent])
                self.assertEqual([d['path'] for d in disks], [str(self.parent)])
                self.assertGreater(disks[0]['free_bytes'], 0)
                self.assertEqual(hardware.candidate_disks(), [])

    def test_hardware_here(self):
        hw = hardware.detect([self.parent])
        self.assertIn(hw['os'], ('macos', 'windows', 'linux'))
        self.assertGreater(hw['ram_bytes'], 2 ** 30)
        self.assertTrue(hw['disks'] and hw['disks'][0]['free_bytes'] > 0)

    def test_mac_free_is_what_finder_calls_available(self):
        """#112: the plain free count leaves out the room macOS makes on its own (purgeable caches, local snapshots)."""
        plain = mock.Mock(total=500 * 10 ** 9, free=17 * 10 ** 9)
        with mock.patch.object(hardware.shutil, 'disk_usage', return_value=plain):
            for platform, mac, free in (('darwin', 140 * 10 ** 9, 140 * 10 ** 9),     # Finder's figure, when macOS gives one
                                        ('darwin', None, 17 * 10 ** 9),               # it cannot say: the plain count
                                        ('darwin', 5 * 10 ** 9, 17 * 10 ** 9),        # never less than the plain count
                                        ('win32', 140 * 10 ** 9, 17 * 10 ** 9)):      # only a Mac asks
                with self.subTest(platform=platform, mac=mac), mock.patch.object(sys, 'platform', platform), \
                        mock.patch.object(hardware, '_mac_available', return_value=mac, create=True) as asked:
                    self.assertEqual(hardware.disk_of(self.parent), (free, 500 * 10 ** 9))
                    self.assertEqual(asked.called, platform == 'darwin')

    @unittest.skipUnless(sys.platform == 'darwin' and importlib.util.find_spec('Foundation'), 'needs a Mac with PyObjC')
    def test_mac_available_here(self):
        """The real call: macOS answers, and its figure is never below the plain free count by more than a moment's writes."""
        n = hardware._mac_available(self.tmp)
        self.assertIsNotNone(n)
        self.assertGreater(n, shutil.disk_usage(self.tmp).free - 10 ** 9)
        self.assertGreaterEqual(hardware.disk_of(self.tmp / 'not yet')[0], shutil.disk_usage(self.tmp).free - 10 ** 9)

    def test_too_little_memory_says_the_whole_need(self):
        """#112: "needs about 14.9 GB ... and this computer has 17.2 GB" read as a fit. Say the total, then what it is made of."""
        import llm_local
        fit = {'status': 'fits', 'ram_total': 16 * 2 ** 30, 'device_bytes': int(14.9e9), 'host_bytes': 0, 'memory': []}
        with mock.patch.object(sys, 'platform', 'darwin'):
            j = llm_local.judge(fit)
        self.assertFalse(j['fits'])
        self.assertEqual(j['why'], 'The local AI needs about 17.6 GB of memory: 14.9 GB for the model and 2.7 GB for everything else. '
                                   'This computer has 17.2 GB.')
        self.assertGreater(j['needs'], fit['ram_total'])


class Through(Base):
    """/api/setup/* over the app's bridge, as the window asks: no socket."""

    def test_bridge(self):
        import bridge as B
        s = self.setup()
        with mock.patch.object(api, 'SETUP', s):
            b = B.Bridge(api.SetupServer, 'buffer', B.ORIGIN_WINDOWS, tmpdir=str(self.tmp))
            got = []
            done = threading.Event()

            def ask(method, path, headers=None, body=None):
                got.clear(); done.clear()
                b.fetch(method, B.ORIGIN_WINDOWS + path, headers or {}, body, lambda r: (got.append(r), done.set()))
                done.wait(20)
                return got[0]
            r = ask('GET', '/api/setup/state')
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.body)['mode'], 'first-run')
            r = ask('POST', '/api/setup/start', {'Content-Type': 'application/json'}, b'{"pack":"small"}')
            self.assertEqual(r.status, 403)       # no launch key
            r = ask('POST', '/api/setup/pause', {'Content-Type': 'application/json', 'X-Lastbook-Key': b.key}, b'{}')
            self.assertEqual(r.status, 200)
            r = ask('GET', '/')
            self.assertEqual(r.status, 200)
            self.assertTrue(b.top_url_ok(B.ORIGIN_WINDOWS + '/ui/setup.html'))
            r = ask('GET', '/documents/x.pdf')
            self.assertEqual(r.status, 404)


@unittest.skipUnless(shutil.which('node'), 'needs Node')
class ModelRecoveryScreen(unittest.TestCase):  # upstream, kept through rebase
    """The real setup error renderer and picker dispatch, without a server or browser dependency."""
    def screen(self, kind='model', done=0, code='network', action=False, cancel=False):
        script = r"""
const fs = require('fs'), src = fs.readFileSync(process.argv[1], 'utf8'), cfg = JSON.parse(process.argv[2]);
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const calls = [], note = {hidden: true}, button = {}, UNIT = 1000;
const $ = s => s === '#haveNote' ? note : button;
const esc = s => String(s == null ? '' : s), ico = () => '', size = String, diskName = () => 'this disk';
const errCode = e => e && e.code, errText = e => e && e.message, cur = () => null, diskFor = () => null;
let folder = '', S = {}, shortfall = null, confirmCancel = false, lastAct = null;
const call = async (path, body) => { calls.push([path, body]); return cfg.cancel ? {cancelled: true} : {path: '/path/to/model.gguf'} };
const useFile = async (path, btn) => { calls.push(['useFile', path, btn === button]) };
const go = () => {}, jobView = () => {}, poll = () => {};
eval(section('  async function haveLibrary(', '  async function useFile('));
eval(section('  function errorBox(', '  function itemHTML('));
eval(section('  async function jobAct(', '  function poll('));
const j = {error: {code: cfg.code}, items: [{kind: cfg.kind, state: 'failed', bytes_done: cfg.done, error: {code: cfg.code}}]};
(async () => { const html = errorBox(j); if (cfg.action) await jobAct('model-file', button); console.log(JSON.stringify({html, calls, note})); })();
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web/setup.js'), json.dumps(
            dict(kind=kind, done=done, code=code, action=action, cancel=cancel))],
            check=True, capture_output=True, text=True, encoding='utf-8')
        return json.loads(r.stdout)

    def test_zero_byte_model_failure_offers_the_file_picker(self):
        html = self.screen()['html']
        self.assertIn('could not reach the local AI download server', html)
        self.assertIn('data-act="model-file"', html)
        self.assertIn('data-act="retry"', html)
        self.assertIn('data-act="back"', html)
        self.assertIn('id="haveNote"', html)

    def test_other_failures_keep_their_existing_wording(self):
        for args in [dict(kind='pack'), dict(done=123), dict(code='checksum')]:
            with self.subTest(args=args):
                html = self.screen(**args)['html']
                self.assertNotIn('data-act="model-file"', html)
        self.assertIn('The internet connection dropped.', self.screen(kind='pack')['html'])

    def test_model_action_reuses_the_file_picker_and_use_path(self):
        self.assertEqual(self.screen(action=True)['calls'], [
            ['pick', {'files': True}], ['useFile', '/path/to/model.gguf', True]])

    def test_cancelling_the_picker_does_not_retry_or_import(self):
        self.assertEqual(self.screen(action=True, cancel=True)['calls'], [['pick', {'files': True}]])


@unittest.skipUnless(shutil.which('node'), 'needs Node')
class UnreachableScreen(unittest.TestCase):
    """setup.js's own errorBox for an 'unreachable' stop (#254): the host, the file's facts to fetch it elsewhere, and the way to
    choose it: #260's Choose AI model file for the model, Choose again for a library file."""
    def box(self, kind):
        script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8'), kind = process.argv[2];
const section = (a, b) => src.slice(src.indexOf(a), src.indexOf(b, src.indexOf(a)));
const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'), ico = () => '', size = n => (n / 1e9).toFixed(1) + ' GB';
const diskName = () => 'this disk', errCode = e => e && e.code, errText = e => e && e.message, cur = () => null, diskFor = () => null;
let folder = '';
eval(section('  function errorBox(', '  function itemHTML('));
const error = {code: 'unreachable', message: 'Lastbook could not reach huggingface.co from this network.', host: 'huggingface.co',
               name: 'model.gguf', size: 16947541728, url: 'https://huggingface.co/x/model.gguf?download=true&a=<b>', sha256: 'ab'.repeat(32)};
console.log(errorBox({error, items: [{kind, state: 'failed', bytes_done: 0, error}]}));
"""
        r = subprocess.run([shutil.which('node'), '-e', script, str(ROOT / 'web/setup.js'), kind], check=True, capture_output=True, text=True, encoding='utf-8')
        return r.stdout

    def test_the_model_offers_its_facts_and_the_picker(self):
        html = self.box('model')
        self.assertIn('Lastbook can’t reach huggingface.co.', html)
        self.assertIn('It may be down for a while', html)                # it may come back: Retry stays, and is not called useless
        self.assertIn('Retry later', html)
        self.assertNotIn('internet connection dropped', html)
        for k in ('File', 'Size', 'Address', 'SHA-256'):
            self.assertIn(f'<dt>{k}</dt>', html)
        self.assertIn('16,947,541,728 bytes', html)                       # exact, to check a copy fetched elsewhere
        self.assertIn('ab' * 32, html)
        self.assertIn('&amp;a=&lt;b>', html)                              # the address is escaped
        self.assertIn('data-act="model-file"', html)
        self.assertIn('data-act="retry"', html)

    def test_a_library_file_points_to_choose_again(self):
        html = self.box('pack')
        self.assertIn('<dt>SHA-256</dt>', html)
        self.assertNotIn('data-act="model-file"', html)
        self.assertIn('Choose a library zip or AI model file', html)


@unittest.skipUnless(shutil.which('node'), 'Node is not installed')
class SkipMap(unittest.TestCase):
    """#267: setup can finish without a state map on Small (no state map) and All (every state's), not on Medium (California's).

    The fixture uses the validated-manifest shape: validate() keeps a published packs[].map only when it
    names a map, so entries that omit it all arrive as None (engine/setup/manifest.py). The skip option
    must still describe each pack's actual result."""
    def run_helpers(self, pack, picked, current=None):
        source = (ROOT / 'web/setup.js').read_text(encoding='utf-8')
        start = source.index('  const packs =')
        helpers = source[start:source.index('  const packDl =', start)]
        state = {'manifest': {'packs': [{'id': 'small', 'map': None}, {'id': 'medium', 'map': None}, {'id': 'all', 'map': None}],
                              'maps': [{'id': 'CA', 'title': 'California'}], 'address_indexes': [{'id': 'CA', 'available': True}]},
                 'current': current}
        script = (f'const S = {json.dumps(state)};\nconst choice = {json.dumps({"pack": pack, "ai": False, "map": picked})};\n' + helpers +
                  '\nconsole.log(JSON.stringify({offered: canSkipMap(), skipped: skippedMap(), missing: mapMissing(), state: selectedState(),'
                  ' map: mapAdds() ? mapAdds().id : null, address: addressAdds() ? addressAdds().id : null}))')
        run = subprocess.run(['node', '-e', script], text=True, capture_output=True, check=True)
        return json.loads(run.stdout)

    def run_switch(self, from_pack, to_pack, picked):
        source = (ROOT / 'web/setup.js').read_text(encoding='utf-8')
        start = source.index('  const packs =')
        helpers = source[start:source.index('  const packDl =', start)]
        state = {'manifest': {'packs': [{'id': 'small', 'map': None}, {'id': 'medium', 'map': None}, {'id': 'all', 'map': None}],
                              'maps': [{'id': 'CA', 'title': 'California'}], 'address_indexes': [{'id': 'CA', 'available': True}]},
                 'current': None}
        script = (f'const S = {json.dumps(state)};\nconst choice = {json.dumps({"pack": from_pack, "ai": False, "map": picked})};\n' + helpers +
                  f'\nsetPack({json.dumps(to_pack)});' +
                  '\nconsole.log(JSON.stringify({kept: choice.map, offered: canSkipMap(), skipped: skippedMap(), missing: mapMissing(), state: selectedState(),'
                  ' map: mapAdds() ? mapAdds().id : null, address: addressAdds() ? addressAdds().id : null}))')
        run = subprocess.run(['node', '-e', script], text=True, capture_output=True, check=True)
        return json.loads(run.stdout)

    def test_small_without_a_map(self):
        self.assertEqual(self.run_helpers('small', None), {'offered': True, 'skipped': False, 'missing': True, 'state': '', 'map': None, 'address': None})
        self.assertEqual(self.run_helpers('small', 'none'), {'offered': True, 'skipped': True, 'missing': False, 'state': '', 'map': None, 'address': None})

    def test_all_without_address_search(self):
        self.assertEqual(self.run_helpers('all', 'none'), {'offered': True, 'skipped': True, 'missing': False, 'state': '', 'map': None, 'address': None})

    def test_medium_keeps_its_own_map(self):   # "none" left over from Small: Medium still has California, as before
        self.assertEqual(self.run_helpers('medium', 'none'), {'offered': False, 'skipped': False, 'missing': False, 'state': 'CA', 'map': None, 'address': 'CA'})

    def test_a_chosen_state_is_unchanged(self):
        self.assertEqual(self.run_helpers('small', 'CA'), {'offered': True, 'skipped': False, 'missing': False, 'state': 'CA', 'map': 'CA', 'address': 'CA'})

    def test_library_with_a_map_offers_no_skip(self):   # Library and local AI: a Small library that already has a state map keeps it
        current = {'pack': 'small', 'map': 'CA', 'address_indexes': ['CA']}
        self.assertEqual(self.run_helpers('small', None, current), {'offered': False, 'skipped': False, 'missing': False, 'state': 'CA', 'map': None, 'address': None})

    def test_switching_from_all_to_small_drops_the_skip(self):   # "no address search" must not become "no local map" by itself
        self.assertEqual(self.run_switch('all', 'small', 'none'),
                         {'kept': None, 'offered': True, 'skipped': False, 'missing': True, 'state': '', 'map': None, 'address': None})

    def test_switching_from_small_to_all_drops_the_skip(self):   # "no local map" must not become "no address search" by itself
        self.assertEqual(self.run_switch('small', 'all', 'none'),
                         {'kept': None, 'offered': True, 'skipped': False, 'missing': True, 'state': '', 'map': None, 'address': None})

    def test_switching_to_medium_drops_the_skip(self):   # the drop is harmless there: Medium resolves California either way
        self.assertEqual(self.run_switch('small', 'medium', 'none'),
                         {'kept': None, 'offered': False, 'skipped': False, 'missing': False, 'state': 'CA', 'map': None, 'address': 'CA'})

    def test_switching_packs_keeps_a_chosen_state(self):
        self.assertEqual(self.run_switch('small', 'all', 'CA'),
                         {'kept': 'CA', 'offered': True, 'skipped': False, 'missing': False, 'state': 'CA', 'map': None, 'address': 'CA'})

    def test_same_pack_keeps_the_skip(self):   # re-entering choose for the same pack changes nothing
        self.assertEqual(self.run_switch('small', 'small', 'none'),
                         {'kept': 'none', 'offered': True, 'skipped': True, 'missing': False, 'state': '', 'map': None, 'address': None})

    def test_validated_manifest_nulls_an_omitted_pack_map(self):   # the shape above is what validate() really outputs
        file = {'name': 'small.zip', 'url': 'https://127.0.0.1/small.zip', 'size': 10, 'sha256': '0' * 64}
        raw = {'version': 1, 'packs': [{'id': 'small', 'title': 'Small', 'files': [file]},
                                       {'id': 'medium', 'title': 'Medium', 'files': [file], 'map': 'CA'}]}
        packs = {p['id']: p for p in manifest.validate(raw, allowed=ALLOWED)['packs']}
        self.assertIsNone(packs['small']['map'])
        self.assertEqual(packs['medium']['map'], 'CA')


@unittest.skipUnless(os.environ.get('LASTBOOK_FULL_DISK') == '1' and sys.platform == 'darwin', 'LASTBOOK_FULL_DISK=1 on a Mac')
class FullDisk(Base):
    def test_disk_fills_during_download(self):
        img = self.tmp / 'small.dmg'
        subprocess.run(['hdiutil', 'create', '-size', '3m', '-fs', 'HFS+', '-volname', 'LBFULL', str(img)], check=True, capture_output=True)
        out = subprocess.run(['hdiutil', 'attach', '-nobrowse', '-mountrandom', str(self.tmp), str(img)], check=True, capture_output=True, text=True).stdout
        mount = Path(out.strip().split('\t')[-1])
        try:
            s = self.setup()
            r = s.start('small', True, str(mount))
            self.assertEqual(r['code'], 'no_space')            # refused before starting, with the numbers
            self.assertLess(r['free_bytes'], r['need_bytes'])
            d = download.Download(f'{self.base}/Lastbook-Small.zip', mount / 'x.zip', len(self.zip), sha(self.zip), download.Control(),
                                  allowed=ALLOWED, context=self.ctx)
            with self.assertRaises(download.Failed) as e:
                d.run()                                       # the space check skipped: the disk really fills
            self.assertEqual(e.exception.code, 'no_space')
        finally:
            subprocess.run(['hdiutil', 'detach', str(mount), '-force'], capture_output=True)


def child(cfg):
    """For the restart tests: run a job until killed."""
    cfg = json.loads(cfg)
    ctx = ssl.create_default_context(cafile=cfg['cert'])
    m = manifest.Manifest(bundled=cfg['manifest'], remote=None, allowed=ALLOWED, context=ctx)
    if cfg.get('slow_unpack'):
        real = install.unpack

        def slow(*a, **k):
            prog = a[4]
            return real(*a[:4], lambda d, t: (time.sleep(0.002), prog(d, t)))
        install.unpack = slow
    s = api.Setup('first-run', cfg['settings'], cfg['parent'], manifest=m, allowed=ALLOWED, context=ctx, home=cfg['home'], llm_local=False)
    print(s.start('small', False), flush=True)
    time.sleep(600)



# ------------------------------------------------------------------------------------- a model server on this computer --
class ModelServerFake(http.server.BaseHTTPRequestHandler):
    """An OpenAI-style server on 127.0.0.1: /v1/models answers with ANSWER (status, body). Counts what it is asked."""
    ANSWER = (200, b'{"object":"list","data":[{"id":"small-9b"},{"id":"other"}]}')
    asked = []

    def do_GET(self):
        ModelServerFake.asked.append(self.path)
        status, body = ModelServerFake.ANSWER
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ModelServer(Base):
    """#78: a model server the person chooses in Setup. Asked only when the person asks, and only on this computer."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = socketserver.ThreadingTCPServer(('127.0.0.1', 0), ModelServerFake)
        cls.fake.daemon_threads = True
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.where = f'127.0.0.1:{cls.fake.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.fake.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        ModelServerFake.ANSWER = (200, b'{"object":"list","data":[{"id":"small-9b"},{"id":"other"}]}')
        ModelServerFake.asked = []
        self.switched = []

    def chooser(self, mode='manage'):
        return self.setup(mode, current=self.tmp if mode == 'manage' else None, on_model_server=lambda b, m: self.switched.append((b, m)))

    def test_fresh_setup_asks_no_model_server(self):
        from engine import model_server
        with mock.patch.object(model_server, 'model_open', side_effect=AssertionError('a model server was asked')):
            for mode in ('first-run', 'manage'):
                st = self.chooser(mode).state()
                self.assertIsNone(st['model_server'])
        self.assertEqual(ModelServerFake.asked, [])
        self.assertFalse((self.settings / 'setup.json').exists() and 'model_server' in json.loads((self.settings / 'setup.json').read_text(encoding='utf-8')))

    def test_lists_the_models_of_the_chosen_server(self):
        s = self.chooser()
        for typed in (self.where, 'localhost:' + self.where.split(':')[1] + '/v1', f'http://{self.where}/', f' http://{self.where}/v1/ '):
            got = s.server_models(typed)
            self.assertEqual(got, {'base': 'http://' + self.where, 'address': self.where, 'models': ['small-9b', 'other']}, typed)
        self.assertEqual(set(ModelServerFake.asked), {'/v1/models'})

    def test_only_this_computer(self):
        from engine import model_server
        s = self.chooser()
        with mock.patch.object(model_server, 'model_open', side_effect=AssertionError('asked')):
            for typed in ('10.0.0.5:8080', 'example.org:80', 'https://127.0.0.1:1', '0.0.0.0:8080', '127.0.0.1:1@example.org:1', '', None, '127.0.0.1', '127.0.0.1:0', '127.0.0.1:70000'):
                got = s.server_models(typed)
                self.assertEqual(got['code'], 'not_here', typed)
                self.assertIn('on this computer', got['error'])
                self.assertEqual(s.use_server(typed, 'small-9b')['code'], 'not_here')
        self.assertEqual(self.switched, [])

    def test_an_unavailable_server_is_named_in_the_error(self):
        with socket.socket() as k:    # a port that was free a moment ago: nothing listens there
            k.bind(('127.0.0.1', 0))
            port = k.getsockname()[1]
        got = self.chooser().server_models(f'127.0.0.1:{port}')
        self.assertEqual(got['code'], 'no_answer')
        self.assertIn(f'127.0.0.1:{port}', got['error'])
        self.assertNotIn('http', got['error'])

    def test_servers_that_do_not_list_models(self):
        for answer, code in (((401, b'{"error":"Invalid API Key"}'), 'needs_key'), ((403, b''), 'needs_key'), ((500, b'x'), 'no_list'),
                             ((200, b'not json'), 'no_list'), ((200, b'{"data":"x"}'), 'no_list'), ((200, b'[1,2]'), 'no_list'),
                             ((200, b'{"data":[]}'), 'no_models'), ((200, b'{"data":[{"name":"no id"}]}'), 'no_models')):
            ModelServerFake.ANSWER = answer
            got = self.chooser().server_models(self.where)
            self.assertEqual(got['code'], code, answer)
            self.assertIn(self.where, got['error'])

    def test_choosing_saves_and_switches(self):
        self.settings.mkdir(parents=True, exist_ok=True)
        (self.settings / 'setup.json').write_text('{"old": ["/path/to/old"]}', encoding='utf-8')
        s = self.chooser()
        self.assertEqual(s.use_server(self.where, 'small-9b'), {'model_server': {'address': self.where, 'model': 'small-9b'}})
        saved = json.loads((self.settings / 'setup.json').read_text(encoding='utf-8'))
        self.assertEqual(saved, {'old': ['/path/to/old'], 'model_server': {'base': 'http://' + self.where, 'model': 'small-9b'}})
        self.assertEqual(self.switched, [('http://' + self.where, 'small-9b')])
        self.assertEqual(api.saved_model_server(self.settings), ('http://' + self.where, 'small-9b'))
        self.assertEqual(self.chooser('first-run').state()['model_server'], {'address': self.where, 'model': 'small-9b'})

        self.assertEqual(s.stop_server(), {'model_server': None})
        self.assertEqual(json.loads((self.settings / 'setup.json').read_text(encoding='utf-8')), {'old': ['/path/to/old']})
        self.assertEqual(self.switched[-1], (None, None))
        self.assertIsNone(api.saved_model_server(self.settings))

    def test_a_model_the_server_does_not_list_is_not_saved(self):
        s = self.chooser()
        for model in ('gone', '', None, ['small-9b']):
            got = s.use_server(self.where, model)
            self.assertEqual(got['code'], 'not_listed', model)
        self.assertIsNone(api.saved_model_server(self.settings))
        self.assertEqual(self.switched, [])

    def test_a_changed_setup_json_cannot_point_off_this_computer(self):
        self.settings.mkdir(parents=True, exist_ok=True)
        for saved in ({'base': 'http://10.0.0.5:8080', 'model': 'm'}, {'base': 'http://example.org:80', 'model': 'm'}, {'base': 'http://127.0.0.1:1'},
                      {'base': 'http://127.0.0.1:1', 'model': 7}, {'base': 'http://127.0.0.1:1', 'model': 'x' * 201}, 'http://127.0.0.1:1', None):
            (self.settings / 'setup.json').write_text(json.dumps({'model_server': saved}), encoding='utf-8')
            self.assertIsNone(api.saved_model_server(self.settings), saved)
            self.assertIsNone(self.chooser().model_server(), saved)

    def test_the_open_library_follows_the_saved_choice(self):
        self.chooser().use_server(self.where, 'small-9b')
        for env, want in (({}, [('http://' + self.where, 'small-9b')]), ({'SURVIVAL_MODEL_BASE': 'http://127.0.0.1:9'}, [])):
            used = []
            with mock.patch.object(api, 'SETUP', self.setup('manage', current=self.tmp)), mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop('SURVIVAL_MODEL_BASE', None)    # test_bridge sets it for the whole run; patch.dict puts it back
                api.follow_model_server(lambda b, m: used.append((b, m)))
                self.assertEqual(used, want, env)
                api.SETUP.use_server(self.where, 'other')     # a choice made now counts, whatever Lastbook was started with
                self.assertEqual(used[-1], ('http://' + self.where, 'other'))
                api.SETUP.use_server(self.where, 'small-9b')

    def test_two_windows_the_last_completed_choice_wins_both_places(self):
        """One window chooses a model while the other stops using it: the saved choice and the running Chat end the same,
        as the action that finished last left them."""
        base = 'http://' + self.where
        for slow in ('switch', 'list'):    # the choosing window is held after saving, or while it asks the server for its models
            with self.subTest(slow=slow):
                self.switched, finished, held, let_go = [], [], threading.Event(), threading.Event()
                s = self.chooser()
                switch, listing = s.on_model_server, api.model_server.list_models

                def slow_switch(b, m):
                    if b and slow == 'switch':
                        held.set()
                        let_go.wait(1)    # the stop waits here once the two are kept in order; otherwise it slips in
                    switch(b, m)

                def slow_list(text):
                    if slow == 'list':
                        held.set()
                        let_go.wait(5)
                    return listing(text)

                s.on_model_server = slow_switch
                with mock.patch.object(api.model_server, 'list_models', side_effect=slow_list):
                    choose = threading.Thread(target=lambda: (s.use_server(self.where, 'small-9b'), finished.append('use')))
                    choose.start()
                    self.assertTrue(held.wait(5))
                    stop = threading.Thread(target=lambda: (s.stop_server(), finished.append('stop')))
                    stop.start()
                    stop.join(0.3)
                    let_go.set()
                    choose.join(10)
                    stop.join(10)

                want = (base, 'small-9b') if finished[-1] == 'use' else None
                self.assertEqual(api.saved_model_server(self.settings), want, finished)
                self.assertEqual(self.switched[-1], want or (None, None), finished)

    def test_routes(self):
        self.assertIn(('POST', 'server-models'), api.ROUTES)
        s = self.chooser()
        self.assertEqual(api.ROUTES[('POST', 'server-models')](s, {'address': self.where})['models'], ['small-9b', 'other'])
        self.assertEqual(api.ROUTES[('POST', 'use-server')](s, {'address': self.where, 'model': 'other'})['model_server']['model'], 'other')
        self.assertEqual(api.ROUTES[('POST', 'stop-server')](s, {}), {'model_server': None})
class ManifestRace(Base):
    """Running from source there is no bundled manifest: the first state() must not answer with an empty list while the fetch runs."""

    EMPTY = {'version': 1, 'published': '', 'packs': [], 'models': [], 'maps': []}

    def empty_setup(self, fetch):
        mf = manifest.Manifest(bundled=dict(self.EMPTY), remote='https://127.0.0.1/manifest-v1.json', allowed=ALLOWED, context=self.ctx)
        with mock.patch.object(download, 'fetch_small', fetch):
            return self.setup(manifest=mf)

    def test_first_state_waits_for_the_fetch(self):
        data = json.dumps(self.manifest().current).encode()

        def slow(*a, **k):
            time.sleep(0.4)
            return data
        s = self.empty_setup(slow)
        with mock.patch.object(download, 'fetch_small', slow):
            st = s.state()
        self.assertEqual([p['id'] for p in st['manifest']['packs']], ['small', 'all'])

    def test_failed_fetch_is_tried_again_by_a_later_state(self):
        data = json.dumps(self.manifest().current).encode()
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            return None if len(calls) == 1 else data
        s = self.empty_setup(flaky)
        with mock.patch.object(download, 'fetch_small', flaky), mock.patch.object(manifest, 'RETRY_S', 0):
            self.assertEqual(s.state()['manifest']['packs'], [])
            self.assertEqual([p['id'] for p in s.state()['manifest']['packs']], ['small', 'all'])

    def test_a_bundled_manifest_never_waits(self):
        mf = self.manifest()
        mf.remote = 'https://127.0.0.1/manifest-v1.json'

        def slow(*a, **k):
            time.sleep(2)
        s = self.setup(manifest=mf)
        with mock.patch.object(download, 'fetch_small', slow):
            t0 = time.time()
            s.state()
        self.assertLess(time.time() - t0, 1.0)

    def test_concurrent_calls_start_one_fetch(self):
        calls = []
        real = threading.Thread

        def slow_thread(*a, **k):
            time.sleep(0.2)          # the window between "no fetch yet" and the fetch being recorded
            return real(*a, **k)

        def fetch(*a, **k):
            calls.append(1)
            time.sleep(0.5)
        mf = manifest.Manifest(bundled=dict(self.EMPTY), remote='https://127.0.0.1/manifest-v1.json', allowed=ALLOWED, context=self.ctx)
        with mock.patch.object(download, 'fetch_small', fetch), mock.patch.object(manifest.threading, 'Thread', slow_thread), \
                mock.patch.object(manifest, 'WAIT_S', 3):
            ts = [real(target=mf.fetch_once) for _ in range(3)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(shutil.which('node'), 'needs node on PATH')
class SetupScreen(unittest.TestCase):
    """web/setup.js run in Node with a fake page (app/tests/setup_js_test.mjs)."""

    def test_screen_asks_again_while_the_list_is_empty(self):
        r = subprocess.run([shutil.which('node'), str(HERE / 'setup_js_test.mjs')], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_background_ask_keeps_keyboard_focus_in_a_real_browser(self):
        """app/tests/setup_focus_browser_test.mjs: setup.html in headless Chrome or Chromium; skipped when neither is found."""
        r = subprocess.run([shutil.which('node'), str(HERE / 'setup_focus_browser_test.mjs')], capture_output=True, text=True, timeout=90)
        if r.returncode == 2:
            self.skipTest(r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--child':
        child(sys.argv[2])
    else:
        unittest.main()
