"""Local retrieval service shared by the web app and the assistant.

Keyword search (the library's own FTS5/BM25 index, read only) + meaning-based search (bge-small-en-v1.5 run locally
with onnxruntime) fused with reciprocal-rank fusion. Every passage carries provenance: catalog id, PDF file page or
video timestamp, and which retriever found it.

The semantic index lives in derived/index/ (SSD): semantic.sqlite (chunks, per-document signatures) and vectors.f16
(append-only float16 rows). It grows incrementally: documents whose catalog signature changed or that are new get
(re)embedded; nothing in library-data is written.
"""
import hashlib, html, json, math, os, re, sqlite3, sys, threading, time
from pathlib import Path

import engine.paths as lastbook_paths
from engine.paths import ro_uri
from engine.saved_pages import caption_blocks
HERE = lastbook_paths.DATA  # holds derived/
if not lastbook_paths.FROZEN and os.name != 'nt' and sys.version_info[:2] == (3, 12):  # Mac binaries that belong to the program, built for 3.12, so they would shadow another Python's numpy; never import code from a data folder
    sys.path.insert(0, str(lastbook_paths.APP / 'derived' / 'pylib'))
LIB = lastbook_paths.LIB
IDX = HERE / 'derived' / 'index'
MODEL = HERE / 'derived' / 'models' / 'bge-small-en-v1.5'
DIM = 384
CHUNK, OVERLAP, MINLEN = 1100, 150, 80
QUERY_PREFIX = 'Represent this sentence for searching relevant passages: '

TELEMETRY = 'onnxruntime not loaded'
# This app stays offline. Read when onnxruntime is imported: the Mac build otherwise opens a usage store and connects to
# Microsoft's collector, whatever disable_telemetry_events says. The Windows build has no such switch: see engine/quiet_onnx.py.
os.environ['ORT_DISABLE_TELEMETRY'] = '1'
try:
    import numpy as np
    AVAILABLE = (MODEL / 'model.onnx').exists()
    if AVAILABLE:  # with no model there is no reason to load it at all
        if os.name == 'nt':  # before the import, which already writes usage events to Windows. If this fails, the import is not reached.
            from engine import quiet_onnx
            quiet_onnx.silence()
        import onnxruntime as ort
        from tokenizers import Tokenizer
        try:
            if hasattr(ort, 'disable_telemetry_events'):
                ort.disable_telemetry_events()
                TELEMETRY = f'ORT_DISABLE_TELEMETRY set, disable_telemetry_events called (onnxruntime {ort.__version__})'
            else:
                TELEMETRY = f'ORT_DISABLE_TELEMETRY set; onnxruntime {ort.__version__} has no disable_telemetry_events'
            if os.name == 'nt':
                TELEMETRY += '; its calls that write events to Windows are replaced before it loads (engine/quiet_onnx.py)'
        except Exception as e:
            TELEMETRY = f'disable_telemetry_events failed: {type(e).__name__}'
except Exception as e:  # semantic search is optional; keyword search keeps working
    AVAILABLE = False
    IMPORT_ERROR = str(e)


class Embedder:
    def __init__(self):
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(2, (os.cpu_count() or 4) // 2)  # leave CPU for the web server and workers
        self.sess = ort.InferenceSession(str(MODEL / 'model.onnx'), so, providers=['CPUExecutionProvider'])
        self.tok = Tokenizer.from_file(str(MODEL / 'tokenizer.json'))
        self.tok.enable_truncation(max_length=320)
        self.tok.enable_padding()
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.lock = threading.Lock()

    def embed(self, texts):
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {'input_ids': ids, 'attention_mask': mask}
        if 'token_type_ids' in self.inputs:
            feed['token_type_ids'] = np.zeros_like(ids)
        with self.lock:
            out = self.sess.run(None, feed)[0]
        v = out[:, 0, :]  # bge uses the [CLS] vector
        v = v / np.linalg.norm(v, axis=1, keepdims=True).clip(1e-9)
        return v.astype(np.float32)


def norm_ws(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def readable(text):
    """Skip table/spreadsheet debris and non-English runs that match every query by accident.

    Words in capitals count too: manuals print their warnings that way ("DO NOT DRINK FLOOD WATER"). A curly
    apostrophe (don’t) keeps a word whole, the same as a straight one."""
    words = re.findall(r"[A-Za-z][A-Za-z'’]+", text)
    if len(words) < 12:
        return False
    letters = sum(len(w) for w in words)
    common = sum(1 for w in words if w.lower() in COMMON)
    return letters / max(1, len(text)) > 0.55 and common / len(words) > 0.18


COMMON = set('the a an of to and in is are be for on with as by or that this it from at not can if your you use when should may will water have has which all but more they their each must after before into out other than any one two do does'.split())


def scores(mat, qv, block=4096):
    """mat (float16 rows) times qv, in float32. numpy has no fast float16 product; turning a block of rows into float32
    at a time is over ten times faster than mat @ qv and adds only one block of memory."""
    qv = np.asarray(qv, dtype=np.float32)
    out = np.empty(len(mat), dtype=np.float32)
    for i in range(0, len(mat), block):
        out[i:i + block] = mat[i:i + block].astype(np.float32) @ qv
    return out


def chunks_of(text):
    text = norm_ws(text)
    if len(text) < MINLEN:
        return []
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + CHUNK)
        if j < len(text):  # end at a sentence or word boundary
            k = text.rfind('. ', i + CHUNK // 2, j)
            j = k + 1 if k > 0 else (text.rfind(' ', i + CHUNK // 2, j) if text.rfind(' ', i + CHUNK // 2, j) > 0 else j)
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - OVERLAP, i + 1)
    return [c for c in out if len(c) >= MINLEN and readable(c)]


def vtt_windows(path, span=45):
    """Timed transcript windows (~45 s) so answers can cite a moment in a video."""
    cues = []
    for start, lines in caption_blocks(path.read_bytes()):
        cur = [start, '']
        for line in lines:
            t = html.unescape(re.sub(r'<[^>]+>', '', line)).strip()
            if t and not (cur[1].endswith(t) and (cur[1] == t or cur[1][-len(t) - 1].isspace())):
                cur[1] = (cur[1] + ' ' + t).strip()
        cues.append(cur)
    out, start, buf, last = [], None, [], ''
    for t, txt in cues:
        if last and txt.startswith(last) and not txt[len(last):len(last) + 1].strip():  # a repeat ends at a word: "So" is not the start of "Some people"
            txt = txt[len(last):].strip()
        last = txt or last
        if not txt:
            continue
        if start is None:
            start = t
        buf.append(txt)
        if t - start >= span:
            out.append((start, ' '.join(buf)))
            start, buf = None, []
    if buf:
        out.append((start or 0, ' '.join(buf)))
    return [(t, norm_ws(x)) for t, x in out if len(x) > 40]


class Index:
    def __init__(self, catalog_fn, trust_fn, fts_db):
        self.catalog_fn, self.trust_fn, self.fts_db = catalog_fn, trust_fn, fts_db
        IDX.mkdir(parents=True, exist_ok=True)
        self.db_path = IDX / 'semantic.sqlite'
        self.vec_path = IDX / 'vectors.f16'
        self._tls = threading.local()
        self._excl = {'mtime': None, 'ids': set()}
        db = self._db()
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE IF NOT EXISTS chunk(id INTEGER PRIMARY KEY, doc_id TEXT, page INTEGER, t REAL, text TEXT, active INTEGER DEFAULT 1);
          CREATE INDEX IF NOT EXISTS chunk_doc ON chunk(doc_id);
          CREATE TABLE IF NOT EXISTS docstate(doc_id TEXT PRIMARY KEY, sig TEXT, nchunks INTEGER, at REAL);
          CREATE TABLE IF NOT EXISTS gaps(doc_id TEXT PRIMARY KEY, reason TEXT);''')
        db.close()
        self.lock = threading.RLock()
        self.emb = Embedder() if AVAILABLE else None
        self.mat = None
        self.active = None
        self.status = {'state': 'starting' if AVAILABLE else 'off', 'chunks': 0, 'docs': 0, 'pending': None, 'rate': None}
        if not AVAILABLE:
            self.status['note'] = 'Meaning-based search is off: no model in this library. Keyword search still works.'
        self._load()

    def _db(self):
        """Writer connection: autocommit, so no transaction is ever left open by accident; writes use tx()."""
        db = sqlite3.connect(self.db_path, timeout=60, isolation_level=None, check_same_thread=False)
        db.execute('PRAGMA busy_timeout=60000')
        return db

    def _ro(self):
        """Per-thread read-only connection, reused (it can never take the write lock)."""
        c = getattr(self._tls, 'ro', None)
        if c is None:
            c = sqlite3.connect(ro_uri(self.db_path), uri=True, timeout=30, isolation_level=None, check_same_thread=False)
            self._tls.ro = c
        return c

    @staticmethod
    def tx(db, fn):
        """Run fn(db) inside BEGIN IMMEDIATE … COMMIT; always roll back on error."""
        db.execute('BEGIN IMMEDIATE')
        try:
            out = fn(db)
            db.execute('COMMIT')
            return out
        except BaseException:
            db.execute('ROLLBACK')
            raise

    def refresh(self):
        """Pick up passages another process (the indexer) appended since we last loaded."""
        try:
            size = self.vec_path.stat().st_size
        except FileNotFoundError:
            return
        if self.mat is not None and size != len(self.mat) * DIM * 2:
            self._load(truncate=False)

    def _load(self, truncate=True, db=None):
        if not self.vec_path.exists() or not AVAILABLE:
            with self.lock:
                self.mat = np.zeros((0, DIM), dtype=np.float16) if AVAILABLE else None
                self.active = np.zeros(0, dtype=bool) if AVAILABLE else None
            return
        raw = np.fromfile(self.vec_path, dtype=np.float16)
        n = len(raw) // DIM
        db = db or self._ro()
        rows = one(db, 'SELECT max(id) FROM chunk')[0] or 0
        n = min(n, rows)  # vectors beyond the committed chunk rows were from an interrupted run
        mat = raw[:n * DIM].reshape(n, DIM)
        act = np.zeros(n, dtype=bool)
        for (i,) in db.execute('SELECT id FROM chunk WHERE active=1 AND id<=?', (n,)).fetchall():
            act[i - 1] = True
        with self.lock:     # both at once: a search meanwhile uses the old pair, never new vectors with the old rows
            self.mat, self.active = mat, act
        if truncate and n * DIM * 2 != self.vec_path.stat().st_size:
            with open(self.vec_path, 'r+b') as f:
                f.truncate(n * DIM * 2)
        self.status.update(chunks=int(act.sum()), docs=one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0])
        if self.status.get('state') == 'starting':
            self.status['state'] = 'loaded'

    # ------------------------------------------------------------ indexing --
    def excluded(self):
        """Video ids the library owner rejected (library-data/video-exclusions.json); never indexed or returned."""
        p = LIB / 'video-exclusions.json'
        try:
            m = p.stat().st_mtime
        except FileNotFoundError:
            return set()
        if m != self._excl['mtime']:
            try:
                ids = set(json.loads(p.read_text(encoding='utf-8')).get('videos', {}))
                self._excl.update(mtime=m, ids=ids | {'metadata-' + i for i in ids} | {i.replace('video-', 'transcript-', 1) for i in ids})
            except (ValueError, OSError):
                pass  # half-written file: keep the previous list
        return self._excl['ids']

    def sources(self):
        """(doc_id, signature, priority, loader) for every text the assistant may read."""
        cat = self.catalog_fn()
        excl = self.excluded()
        out = []
        for r in cat['by_id'].values():
            if r['id'] in excl:
                continue
            k = r['kind']
            if k in ('pdf', 'html', 'pptx'):
                sig = f"{r.get('sha256')}:{r.get('pages')}"
                out.append((r['id'], sig, -self.trust_fn(r)[1], ('pages', r)))
            elif k == 'video':
                tx = cat['video_tx'].get(r['path'])
                vtts = ([tx['vtt_path']] if tx and tx.get('vtt_path') else []) + [c for c in r.get('captions', []) if c.endswith('.vtt')]
                vtt = next((v for v in vtts if (LIB / v).is_file()), None)
                if vtt:
                    out.append((r['id'], 'vtt:payload-2:' + vtt, -self.trust_fn(r)[1], ('vtt', vtt)))
                elif tx and (LIB / tx['path']).is_file():
                    out.append((r['id'], 'tx:' + tx['sha256'], -self.trust_fn(r)[1], ('tx', tx)))
        out.sort(key=lambda x: x[2])
        return out

    def _texts(self, doc_id, how):
        kind, ref = how
        if kind == 'pages':
            con = sqlite3.connect(ro_uri(self.fts_db), uri=True)
            rows = con.execute('SELECT page, body FROM pages WHERE doc_id=? ORDER BY CAST(page AS INTEGER)', (doc_id,)).fetchall()
            con.close()
            if not rows:  # not in the keyword index (yet); fall back to the extracted text file
                p = LIB / (ref.get('text_path') or f'text/{doc_id}.txt')
                if p.is_file():
                    t = p.read_text(encoding='utf-8', errors='replace')
                    rows = list(enumerate(t.split('\n\f\n') if '\n\f\n' in t else t.split('\f'), 1))
            return [(int(p), None, c) for p, b in rows for c in chunks_of(b)]
        if kind == 'vtt':
            return [(None, t, txt) for t, txt in vtt_windows(LIB / ref)]
        return [(None, None, c) for c in chunks_of((LIB / ref['path']).read_text(encoding='utf-8', errors='replace'))]

    def update(self, budget_s=None, batch=48):
        """Embed new/changed documents. Returns number of documents processed."""
        if not self.emb:
            self.status['state'] = 'off'
            return 0
        lockf = open(IDX / 'indexer.lock', 'w')
        try:
            lastbook_paths.try_lock(lockf)
        except OSError:  # another process is indexing; just follow its progress
            self.refresh()
            self.status['state'] = 'indexing (other process)'
            return -1
        try:
            db = self._db()
            try:
                self._reconcile(db)
                self._load(truncate=True, db=db)
            finally:
                db.close()
            return self._update(budget_s, batch)
        finally:
            lastbook_paths.unlock(lockf)
            lockf.close()

    def _reconcile(self, db):
        """Vectors and passage rows must line up 1:1. If an earlier run left rows without vectors, drop those rows
        and mark their documents for re-embedding instead of refusing to index."""
        vec_rows = self.vec_path.stat().st_size // (DIM * 2) if self.vec_path.exists() else 0
        rows = one(db, 'SELECT max(id) FROM chunk')[0] or 0
        if rows <= vec_rows:
            return 0
        docs = [d for (d,) in db.execute('SELECT DISTINCT doc_id FROM chunk WHERE id>?', (vec_rows,)).fetchall()]

        def w(db):
            db.execute('DELETE FROM chunk WHERE id>?', (vec_rows,))
            for d in docs:
                db.execute('UPDATE chunk SET active=0 WHERE doc_id=?', (d,))
                db.execute('DELETE FROM docstate WHERE doc_id=?', (d,))
        self.tx(db, w)
        self.status['repaired'] = {'rows_without_vectors': rows - vec_rows, 'documents_requeued': docs[:20],
                                   'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        return rows - vec_rows

    def _update(self, budget_s, batch):
        db = self._db()
        try:
            return self._update_with(db, budget_s, batch)
        finally:
            db.close()

    def _hide(self, db, doc_ids, forget_text=False):
        """Deactivate every passage of these documents (removed, rejected or replaced)."""
        ids = [i for d in doc_ids for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (d,)).fetchall()]

        def w(db):
            for d in doc_ids:
                db.execute("UPDATE chunk SET active=0" + (", text=''" if forget_text else '') + " WHERE doc_id=?", (d,))
                db.execute('DELETE FROM docstate WHERE doc_id=?', (d,))
                if forget_text:
                    db.execute('DELETE FROM gaps WHERE doc_id=?', (d,))
        self.tx(db, w)
        with self.lock:
            for i in ids:
                if i <= len(self.active):
                    self.active[i - 1] = False
        return len(ids)

    def _update_with(self, db, budget_s, batch):
        known = dict(db.execute('SELECT doc_id, sig FROM docstate').fetchall())
        srcs = self.sources()
        excl = self.excluded()
        todo = [s for s in srcs if known.get(s[0]) != s[1]]
        live = {s[0] for s in srcs}
        # Prune: documents gone from the catalog, and rejected videos (also any stray passages they left behind).
        stale = [d for d in known if d not in live]
        stray = [d for (d,) in db.execute(f"SELECT DISTINCT doc_id FROM chunk WHERE active=1 AND doc_id IN ({','.join('?' * len(excl))})", list(excl)).fetchall()] if excl else []
        pruned = 0
        if stale or stray:
            rejected = [d for d in set(stale) | set(stray) if d in excl]
            pruned += self._hide(db, [d for d in stale if d not in excl])
            pruned += self._hide(db, rejected, forget_text=True)
        self.status.update(state='indexing' if todo else 'up to date', pending=len(todo), pruned_last=pruned)
        t0, done, nchunks = time.time(), 0, 0
        for doc_id, sig, _, how in todo:
            if budget_s and time.time() - t0 > budget_s:
                break
            gap = None
            try:
                parts = self._texts(doc_id, how)
            except Exception as e:
                parts, gap = [], f'unreadable: {e}'[:200]
            if not parts and not gap:
                gap = 'no extractable text'
            vecs = [self.emb.embed([p[2] for p in parts[i:i + batch]]) for i in range(0, len(parts), batch)]
            v = np.concatenate(vecs).astype(np.float16) if parts else None
            with self.lock:
                old = [i for (i,) in db.execute('SELECT id FROM chunk WHERE doc_id=? AND active=1', (doc_id,)).fetchall()]
                start = (one(db, 'SELECT max(id) FROM chunk')[0] or 0) + 1
                if start - 1 != len(self.mat):  # vectors and rows drifted (e.g. an interrupted run): realign, don't fail
                    self._reconcile(db)
                    self._load(truncate=True, db=db)
                    start = (one(db, 'SELECT max(id) FROM chunk')[0] or 0) + 1
                    if start - 1 != len(self.mat):
                        raise RuntimeError(f'vector file has {len(self.mat)} rows but the database has {start - 1}; rebuild derived/index')

                def w(db):
                    db.execute('UPDATE chunk SET active=0 WHERE doc_id=?', (doc_id,))
                    if parts:
                        db.executemany('INSERT INTO chunk(id,doc_id,page,t,text,active) VALUES (?,?,?,?,?,1)',
                                       [(start + j, doc_id, p[0], p[1], p[2]) for j, p in enumerate(parts)])
                    if gap:
                        db.execute('INSERT OR REPLACE INTO gaps VALUES (?,?)', (doc_id, gap))
                    else:
                        db.execute('DELETE FROM gaps WHERE doc_id=?', (doc_id,))
                    db.execute('INSERT OR REPLACE INTO docstate VALUES (?,?,?,?)', (doc_id, sig, len(parts), time.time()))
                    if parts:  # append vectors last, inside the transaction: a failed write never leaves orphan rows
                        with open(self.vec_path, 'ab') as f:
                            f.write(v.tobytes())
                self.tx(db, w)
                if parts:
                    self.mat = np.concatenate([self.mat, v]) if len(self.mat) else v
                    self.active = np.concatenate([self.active, np.ones(len(v), dtype=bool)])
                for i in old:
                    self.active[i - 1] = False
            done += 1
            nchunks += len(parts)
            el = time.time() - t0
            self.status.update(pending=len(todo) - done, chunks=int(self.active.sum()),
                               rate=round(nchunks / el, 1) if el > 1 else None)
        self.status.update(state='up to date' if done == len(todo) else 'indexing',
                           docs=one(db, 'SELECT count(*) FROM docstate WHERE nchunks>0')[0])
        return done

    def run_forever(self, every=300):
        while True:
            try:
                n = self.update(budget_s=None)
                if n >= 0:  # a successful pass clears any earlier error
                    self.status.pop('error', None)
                    self.status['last_ok'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            except Exception as e:
                print(f'semantic index: {e}', file=sys.stderr, flush=True)
                self.status.update(state='error', error=type(e).__name__, error_at=time.strftime('%Y-%m-%dT%H:%M:%S'))  # shown to clients: no paths
            time.sleep(every)


    # ------------------------------------------------------------- search --
    def semantic(self, q, k=60, allowed=None, floor=0.55):
        if not self.emb or self.mat is None or not len(self.mat):
            return []
        qv = self.emb.embed([QUERY_PREFIX + q])[0]
        with self.lock:
            m, act = self.mat, self.active
            s = scores(m, qv)
            s[~act[:len(s)]] = -1
        top = np.nonzero(s >= floor)[0]  # everything above the floor, best first: filtering by allowed happens before any cut
        top = top[np.argsort(-s[top])]
        db = self._ro()
        excl = self.excluded()
        out, seen = [], {}
        for b in range(0, len(top), 500):  # doc ids in batches, text only for what survives the filters
            ids = [int(i) + 1 for i in top[b:b + 500]]
            docs = dict(db.execute(f"SELECT id,doc_id FROM chunk WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
            for i in top[b:b + 500]:
                d = docs.get(int(i) + 1)
                if d is None or d in excl:
                    continue
                if d not in seen:
                    seen[d] = not allowed or bool(allowed(d))
                if not seen[d]:
                    continue
                row = one(db, 'SELECT doc_id,page,t,text FROM chunk WHERE id=?', (int(i) + 1,))
                if not row or not readable(row[3]):  # indexed before the quality filter existed
                    continue
                out.append({'doc_id': row[0], 'page': row[1], 't': row[2], 'text': row[3], 'sem': float(s[i])})
                if len(out) >= k:
                    return out
        return out

    def gaps_for(self, doc_ids):
        db = self._ro()
        return dict(db.execute(f"SELECT doc_id, reason FROM gaps WHERE doc_id IN ({','.join('?' * len(doc_ids))})", list(doc_ids)).fetchall()) if doc_ids else {}


def one(db, sql, args=()):
    """First row of a query, always stepping the statement to completion (an unfinished statement keeps an old
    snapshot of the database open in WAL mode, which is how stale row counts crept in before)."""
    rows = db.execute(sql, args).fetchall()
    return rows[0] if rows else None


def rrf(lists, k=60):
    scores = {}
    for name, items in lists:
        items = list(dict.fromkeys(items))  # a page split into several passages counts once, at its best rank
        for rank, key in enumerate(items):
            e = scores.setdefault(key, {'score': 0.0, 'ranks': {}})
            e['score'] += 1 / (k + rank + 1)
            e['ranks'][name] = rank + 1
    return scores


if __name__ == '__main__':  # python retrieval.py  -> build/refresh the semantic index once
    sys.path.insert(0, str(lastbook_paths.APP))
    import server
    ix = Index(server.catalog, server.trust, server.library.DB)
    print('pending documents:', len([1 for s in ix.sources()]), flush=True)
    stop = threading.Event()
    def report():
        while not stop.wait(30):
            print(json.dumps(ix.status), flush=True)
    threading.Thread(target=report, daemon=True).start()
    n = ix.update()
    stop.set()
    print('done', n, json.dumps(ix.status), flush=True)
