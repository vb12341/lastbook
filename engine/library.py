#!/usr/bin/env python3
"""Offline reference catalog, page-level search and local media/ZIM reader.

  python3 engine/library.py build
  python3 engine/library.py search 'water purification'
  python3 engine/library.py serve --port 8879

Lastbook's own copy. The library folder comes from LASTBOOK_LIBRARY, or library-data beside the engine folder.
"""
from __future__ import annotations
import argparse, collections, concurrent.futures, hashlib, html, http.server, importlib.util, json, mimetypes, os, re, shutil, sqlite3, subprocess, sys, threading, time, urllib.parse, zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

if __package__:
 from .saved_pages import caption_blocks
else:  # direct execution: python3 engine/library.py build
 from saved_pages import caption_blocks

FROZEN=bool(getattr(sys,'frozen',False))
ROOT=Path(os.environ['LASTBOOK_LIBRARY']).resolve() if os.environ.get('LASTBOOK_LIBRARY') else (Path(__file__).resolve().parents[1]/'library-data').resolve()
VENDOR=ROOT/'tools'/('vendor-py312' if sys.version_info[:2]==(3,12) else 'vendor')
if not FROZEN and VENDOR.is_dir():sys.path.insert(0,str(VENDOR))  # the packaged app never imports code from the data folder
try:
 from bs4 import BeautifulSoup
except ImportError:BeautifulSoup=None
PDFTOTEXT=shutil.which('pdftotext')
if os.environ.get('SURVIVAL_PDF_ENGINE')=='pdfium':PDFTOTEXT=None
MAX_PARSED_PAGE=8*1024**2  # parsing a page to rewrite its links takes about 100 times its size in memory and seconds of time; a larger one is served as it is
PDF_LOCK=threading.Lock()  # PDFium is not thread-safe
CLIENT_GONE=(BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError)
# The library folders a file may be served from. One may itself be a link to another disk (videos-external).
SERVED_FOLDERS=('documents','pages','videos','videos-external','transcripts','metadata/videos','courses','collections','assets')
try:from engine import safe_paths
except ImportError:  # run as a script (python3 engine/library.py): the engine folder is on the path, not the project
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]));from engine import safe_paths
from engine.safe_paths import RESERVED_NAMES,real_path,parts_below,name_refused,segments_ok  # noqa: E402  (also used as library.segments_ok)

def load_remote_storage():
 """Videos this Mac has moved to other machines. That code stays in the library's own tools and is never packaged."""
 path=ROOT/'tools'/'remote_storage.py'
 if FROZEN or not path.is_file():return None
 writes=sys.dont_write_bytecode
 sys.dont_write_bytecode=True  # no compiled cache written into the library
 try:
  spec=importlib.util.spec_from_file_location('remote_storage',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  return module
 except Exception:return None  # e.g. no fcntl on Windows
 finally:sys.dont_write_bytecode=writes
_remote=load_remote_storage()
remote_records=_remote.records if _remote else (lambda:{})
serve_remote=_remote.serve if _remote else None

def folder_refused(parts):
 """parts: below the library's top. Hidden folders stay out everywhere. These four names are the library's own working folders;
 inside a served folder they are just names (a course has a page called tools), and what is served is decided by being there."""
 parts=[x.lower() for x in parts]
 if any(x.startswith('.') for x in parts):return True
 if any('/'.join(parts).startswith(f+'/') for f in SERVED_FOLDERS):return False
 return any(x in ('tools','logs','work','runtime') for x in parts)

def parts_refused(parts,folders=False):
 """Run on what a path really resolves to: a link can end at "x.info.json:s", which the name check alone would pass."""
 return name_refused(parts) or (folders and folder_refused(parts)) or not segments_ok('/'.join(parts))

def zim_file(name):
 """The file behind a /zim/<name>/ address, after the same checks as any other file; ValueError if refused.
 engine/safe_paths.py holds the check, so Chat's encyclopedia search and citations refuse exactly what this does."""
 return safe_paths.zim_file(ROOT,name)

def zim_main_path(arc):
 """main_entry is usually a redirect whose own path ('mainPage') cannot be looked up. Follow it."""
 entry=arc.main_entry
 for _ in range(8):
  if not entry.is_redirect:break
  entry=entry.get_redirect_entry()
 return entry.path
DB=ROOT/'search.sqlite'
STYLE='''body{font:17px/1.65 system-ui,sans-serif;color:#202b30;background:#f8f6ef;max-width:1100px;margin:32px auto;padding:0 24px}a{color:#166456}aside{background:#e6ede4;padding:14px;margin-bottom:26px;font-size:14px}table{border-collapse:collapse;max-width:100%;overflow:auto;display:block}td,th{border:1px solid #bbc4bb;padding:8px}img,video{max-width:100%;height:auto}h1,h2,h3{line-height:1.2}pre{white-space:pre-wrap}'''

def catalog():
 records=[];bad=[]
 remote=remote_records()
 for manifest in sorted((ROOT/'manifests').glob('*.json')):
  if any(x in manifest.stem for x in ['failures','progress','candidates','catalog','validation','summary']):continue
  try:data=json.loads(manifest.read_text(encoding='utf-8'))
  except Exception:continue
  if not isinstance(data,list):continue
  for r in data:
   if not isinstance(r,dict) or not r.get('path') or not r.get('title'):continue
   p=ROOT/r['path']
   if not p.is_file() and r['path'] not in remote:bad.append({'path':r['path'],'reason':'missing file'});continue
   if p.suffix in ['.part','.tmp']:continue
   r=dict(r);r['path']=p.relative_to(ROOT).as_posix()
   r.setdefault('kind',p.suffix.lstrip('.'));r.setdefault('category','reference');r.setdefault('collection','Reference')
   r.setdefault('publisher','See source');r.setdefault('source_url',r.get('url',''))
   r['bytes']=remote[r['path']]['bytes'] if r['path'] in remote else p.stat().st_size
   if r.get('pages') is None:r.pop('pages',None)
   r.setdefault('id',hashlib.sha256(r['path'].encode()).hexdigest()[:16])
   records.append(r)
 unique={}
 for r in records:
  key=r.get('sha256') or r['path']
  if key not in unique:unique[key]=r
  else:
   winner=unique[key]
   winner.setdefault('also_in',[]).append(r['collection'])
   winner.setdefault('alternate_paths',[]).append(r['path'])
 records=sorted(unique.values(),key=lambda r:(r['category'],r['collection'],r['title']))
 atomic_write(ROOT/'catalog.json',json.dumps(records,indent=2))   # the app knows a library by this file: never half of it
 return records,bad

def extract(r):
 path=ROOT/r['path'];kind=r['kind'];uid=r['id'];textpath=ROOT/'text'/f'{uid}.txt';meta=textpath.with_suffix('.json')
 if kind=='zim':return r,[],None
 ocr=ROOT/'ocr'/f'{uid}.json'
 if kind=='pdf' and ocr.exists():
  try:
   data=json.loads(ocr.read_text(encoding='utf-8'))
   pages=[p.get('text','') for p in data.get('pages',[])] if data.get('sha256')==r.get('sha256') else []
   if not all(isinstance(p,str) for p in pages):pages=[]
  except (OSError,ValueError,TypeError,AttributeError):pages=[]  # use the original when the OCR cache is unreadable
  if sum(len(p) for p in pages)>100:
   r.update(pages=len(pages),searchability='OCR text: verify against original page',ocr_path=ocr.relative_to(ROOT).as_posix())
   textpath.write_text('\n\f\n'.join(pages),encoding='utf-8');r['text_path']=textpath.relative_to(ROOT).as_posix()
   return r,pages,None
 video=kind in ['video','mp4','webm','mkv']
 if video:
  try:
   captions=[(c,(ROOT/c).read_bytes()) for c in r.get('captions',[])]
  except Exception as e:
   # A failed refresh must not remove usable cached pages from the rebuilt index.
   try:
    m=json.loads(meta.read_text(encoding='utf-8'));details=m.get('details',{})
    if isinstance(details,dict) and m.get('sha256')==r.get('sha256'):
     pages=textpath.read_text(encoding='utf-8').split('\n\f\n')
     if any(page.strip() for page in pages):
      r.update(details);r['text_path']=textpath.relative_to(ROOT).as_posix()
      return r,pages,str(e)
   except (OSError,ValueError,TypeError,AttributeError):pass  # missing or unreadable cache
   return r,[],str(e)
  # A video's checksum says nothing about its subtitles. Version 2 also retires caches made before the cue-boundary fixes.
  caption_inputs={'version':2,'files':[[c,hashlib.sha256(data).hexdigest()] for c,data in captions],
                  'fallback':[r['title'],r.get('notes','')]}
 if meta.exists() and textpath.exists():
  try:
   m=json.loads(meta.read_text(encoding='utf-8'));details=m.get('details',{})
   if isinstance(details,dict) and m.get('sha256')==r.get('sha256') and (not video or m.get('caption_inputs')==caption_inputs):
    text=textpath.read_text(encoding='utf-8');pages=text.split('\n\f\n')
    if not video or any(page.strip() for page in pages):
     r.update(details);r['text_path']=textpath.relative_to(ROOT).as_posix();return r,pages,None
  except (OSError,ValueError,TypeError,AttributeError):pass  # an unreadable cache is rebuilt from the original below
 pages=[]
 try:
  if kind=='pdf':
   if PDFTOTEXT:
    result=subprocess.run([PDFTOTEXT,'-layout','-enc','UTF-8',str(path),'-'],capture_output=True,timeout=120)
    if result.returncode:raise ValueError(result.stderr.decode(errors='replace')[-500:])
    text=result.stdout.decode('utf-8',errors='replace');pages=text.split('\f')
    if pages and not pages[-1].strip():pages.pop()
   else:
    import pypdfium2
    with PDF_LOCK:
     pdf=pypdfium2.PdfDocument(str(path))
     try:pages=[page.get_textpage().get_text_bounded() or '' for page in pdf]
     finally:pdf.close()
   r['pages']=len(pages)
   r['searchability']='text' if sum(len(p.strip()) for p in pages)>100 else 'image-only: needs OCR'
  elif kind in ['html','htm']:
   doc=path.read_text(encoding='utf-8',errors='replace')
   if BeautifulSoup:
    soup=BeautifulSoup(doc,'html.parser')
    for t in soup.select('script,style,nav,footer,header,form'):t.decompose()
    pages=[soup.get_text('\n',strip=True)]
   else:pages=[html.unescape(re.sub('<[^>]+>',' ',doc))]
  elif kind=='pptx':
   with zipfile.ZipFile(path) as z:
    for n in sorted([n for n in z.namelist() if re.match(r'ppt/slides/slide\d+.xml$',n)],key=lambda n:int(re.search(r'slide(\d+)',n).group(1))):
     pages.append('\n'.join(e.text or '' for e in ET.fromstring(z.read(n)).iter() if e.tag.endswith('}t')))
   r['slides']=len(pages)
  elif video:
   text=[]
   for _,data in captions:
    for _,lines in caption_blocks(data):
     for line in lines:
      line=html.unescape(re.sub('<[^>]+>','',line)).strip()
      if line and (not text or text[-1]!=line):text.append(line)
   pages=['\n'.join(text)] if text else [r['title']+'\n'+r.get('notes','')]
  else:pages=[path.read_text(encoding='utf-8',errors='replace')]
  textpath.write_text('\n\f\n'.join(pages),encoding='utf-8')
  cached={'sha256':r.get('sha256'),'details':{k:r[k] for k in ['pages','slides','searchability'] if k in r}}
  if video:cached['caption_inputs']=caption_inputs
  meta.write_text(json.dumps(cached),encoding='utf-8')
  r['text_path']=textpath.relative_to(ROOT).as_posix()
  return r,pages,None
 except Exception as e:return r,[],str(e)

def build():
 (ROOT/'text').mkdir(exist_ok=True)
 records,bad=catalog();building=DB.with_suffix('.building.sqlite');con=sqlite3.connect(building)
 con.execute('DROP TABLE IF EXISTS pages')
 con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
 built=[];errors=[];totalpages=0
 with concurrent.futures.ThreadPoolExecutor(4) as pool:
  for i,(r,pages,error) in enumerate(pool.map(extract,records)):
   built.append(r)
   if error:errors.append({'path':r['path'],'error':error})
   for page,body in enumerate(pages,1):
    if body.strip():con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)',(r['id'],r['title'],r['collection'],r['category'],r['path'],page,body))
   totalpages+=len(pages)
   if i%100==0:print(f'Indexed {i+1}/{len(records)} resources',flush=True)
 con.commit();con.close();building.replace(DB)
 atomic_write(ROOT/'catalog.json',json.dumps(built,indent=2))
 summary={'resources':len(built),'pdfs':sum(r['kind']=='pdf' for r in built),'pdf_pages':sum(r.get('pages') or 0 for r in built),'web_pages':sum(r['kind'] in ['html','htm'] for r in built),'videos':sum(r['kind'] in ['video','mp4','webm','mkv'] for r in built),'collections':sum(r['kind']=='zim' for r in built),'document_bytes':sum(r['bytes'] for r in built),'indexed_units':totalpages,'categories':dict(collections.Counter(r['category'] for r in built)),'extraction_errors':errors,'missing_files':bad,'image_only_pdfs':[r['path'] for r in built if (r.get('searchability') or '').startswith('image-only')]}
 atomic_write(ROOT/'summary.json',json.dumps(summary,indent=2));(ROOT/'reader.css').write_text(STYLE,encoding='utf-8')
 write_index(built,summary)
 print(json.dumps({k:v for k,v in summary.items() if k not in ['extraction_errors','missing_files','image_only_pdfs']},indent=2))

def rewrite_links(records):
 mapping={}
 for r in records:
  for key in ['source_url','final_url','url']:
   if r.get(key):mapping[urllib.parse.urldefrag(r[key])[0]]=r['path']
 if not BeautifulSoup:return
 count=0
 for r in records:
  if r['kind'] not in ['html','htm']:continue
  path=ROOT/r['path'];s=BeautifulSoup(path.read_text(encoding='utf-8',errors='replace'),'html.parser');changed=False
  for a in s.select('a[href]'):
   base,fragment=urllib.parse.urldefrag(a['href']);target=mapping.get(base)
   if target:
    a['href']=os.path.relpath(ROOT/target,path.parent)+('#'+fragment if fragment else '');changed=True
  if changed:path.write_text(str(s),encoding='utf-8');count+=1
 print(f'Linked {count} archived pages to local references',flush=True)

def search(query,limit=25):
 terms=re.findall(r'[\w-]+',query,flags=re.UNICODE)
 if not terms:return []
 expression=' AND '.join('"'+t.replace('"','""')+'"' for t in terms)
 con=sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
 rows=con.execute("SELECT doc_id,title,collection,category,path,page,snippet(pages,6,'[',']',' … ',42) AS excerpt,bm25(pages,0,5,2,1,0,0,1) AS score FROM pages WHERE pages MATCH ? ORDER BY score LIMIT ?",(expression,limit)).fetchall()
 con.close();return [dict(r) for r in rows]

def write_index(records,summary):
 payload=json.dumps([{k:r.get(k) for k in ['id','title','collection','category','publisher','path','kind','pages','duration','bytes','notes','source_url']} for r in records]).replace('</','<\\/')
 doc='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Survival & Rebuilding Library</title><style>'''+STYLE+'''body{max-width:1400px}h1{font-size:42px;margin-bottom:12px}.lead{max-width:860px;color:#53645c}.tools{position:sticky;top:0;background:#f8f6ef;padding:12px 0;display:flex;gap:10px;flex-wrap:wrap;border-bottom:1px solid #cbd2c7}input,select,button{font:inherit;padding:10px;border:1px solid #a8b9aa;border-radius:6px}input{flex:1;min-width:230px}button{background:#1d5c48;color:white;cursor:pointer}.card{border-bottom:1px solid #d4dacf;padding:18px 0;display:grid;grid-template-columns:1fr auto;gap:15px}.tag{font-size:13px;letter-spacing:.04em;color:#5e7064;text-transform:uppercase}.title{font-size:20px;font-weight:600;text-decoration:none}.details{color:#586a60;font-size:14px}.stats{background:#e6ede4;padding:18px;border-radius:8px;margin:22px 0}#results{white-space:normal}.excerpt{font-size:15px;max-width:1000px}.note{font-size:14px;color:#65746a}dialog{max-width:900px;width:85%;border:1px solid #adb9aa;border-radius:10px}video{width:100%}</style></head><body>
<div class="tag">Offline reference collection</div><h1>Survival & Rebuilding Library</h1>
<p class="lead">Field survival, emergency care, water, food production, engineering and the science needed to understand them. Original manuals and illustrations, archived websites, video demonstrations, and complete reference collections.</p>
<div class="stats">__STATS__</div>
<p class="note">Sources are preserved as reference material, not individually endorsed procedures. Check edition dates and professional prerequisites. Historical material can conflict with current medical, food-safety, engineering or regulatory guidance. Captions and extracted text may contain errors; inspect the original page or video.</p>
<div class="tools"><input id="q" placeholder="Search titles, topics, publishers…"><select id="category"><option value="">All subjects</option></select><select id="kind"><option value="">All formats</option><option value="pdf">PDF manuals</option><option value="html">Archived web pages</option><option value="video">Actual videos</option><option value="transcript">Video transcripts</option><option value="metadata">Video metadata</option><option value="zim">Full offline collections</option></select><button id="full">Search full text</button></div>
<p id="status"></p><div id="results"></div>
<dialog id="player"><button onclick="document.getElementById('player').close();document.querySelector('video').pause()">Close</button><h2 id="videoTitle"></h2><video controls></video></dialog>
<details><summary>Using this with DeepSeek</summary><p>Start the local reader with <code>python3 tools/library.py serve --port 8879</code>. Search from a tool-enabled model with <code>python3 tools/library.py search "your topic"</code>, or GET <code>http://127.0.0.1:8879/api/search?q=your+topic</code>. Page numbers in results refer to the PDF file page, not necessarily the printed page label. Cite the exact source and page; open the original for tables and diagrams. Downloading these files does not automatically attach them to a model.</p></details>
<script>const docs=__DATA__;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
for(const c of [...new Set(docs.map(d=>d.category))].sort()){$('category').insertAdjacentHTML('beforeend',`<option>${esc(c)}</option>`)}
function render(){const q=$('q').value.toLowerCase(),cat=$('category').value,kind=$('kind').value;const list=docs.filter(d=>(!cat||d.category===cat)&&(!kind||d.kind===kind)&&(!q||[d.title,d.collection,d.publisher,d.category].join(' ').toLowerCase().includes(q)));$('status').textContent=`${list.length.toLocaleString()} resources shown`;$('results').innerHTML=list.slice(0,500).map(d=>{let href=d.kind==='zim'?`/zim/${encodeURIComponent(d.path.split('/').pop())}/`:d.path;return `<div class="card"><div><div class="tag">${esc(d.category)} · ${esc(d.kind)}</div><a class="title" href="${esc(href)}" ${d.kind==='video'?`data-video="${esc(d.id)}"`:''}>${esc(d.title)}</a><div class="details">${esc(d.collection)} · ${esc(d.publisher)}${d.pages?' · '+d.pages+' pages':''}${d.duration?' · '+Math.round(d.duration/60)+' min':''}</div></div><span class="details">${(d.bytes/1048576).toFixed(1)} MiB</span></div>`}).join('');for(const a of document.querySelectorAll('[data-video]'))a.onclick=e=>{e.preventDefault();const d=docs.find(d=>d.id===a.dataset.video);$('videoTitle').textContent=d.title;document.querySelector('video').src=d.path;$('player').showModal()}}
for(const id of ['q','category','kind'])$(id).addEventListener(id==='q'?'input':'change',render);render();$('full').onclick=async()=>{if(location.protocol==='file:'){$('status').textContent='Full-text search and collection reading need the local server. Run python3 tools/library.py serve --port 8879, then open http://127.0.0.1:8879';return}try{$('status').textContent='Searching archived pages…';const data=await fetch('/api/search?q='+encodeURIComponent($('q').value)).then(r=>r.json());$('status').textContent=`${data.length} matching pages`;$('results').innerHTML=data.map(d=>`<div class="card"><div><a class="title" href="${esc(d.path)}${d.path.endsWith('.pdf')?'#page='+d.page:''}">${esc(d.title)} — page ${d.page}</a><div class="details">${esc(d.collection)} · ${esc(d.category)}</div><p class="excerpt">${esc(d.excerpt)}</p></div></div>`).join('')}catch(e){$('status').textContent='Search unavailable: '+e.message}};</script></body></html>'''
 stats=f"{summary['pdfs']:,} PDF documents · {summary['pdf_pages']:,} PDF pages · {summary['web_pages']:,} archived web pages · {summary['videos']:,} videos · {summary['collections']} complete collections"
 doc=doc.replace('<button id="full">Search full text</button>','<button id="full">Search manuals</button><button id="encyclopedias">Search encyclopedias</button>')
 doc=doc.replace('<div class="tools">','<p id="downloadProgress" class="note"></p><div class="tools">')
 if (ROOT/'coverage-audit.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="coverage-audit.html">Coverage audit: strengths, gaps, and next priorities</a></p><div class="stats">')
 if (ROOT/'audit-closure.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="audit-closure.html">Updated coverage: survival through advanced engineering, with remaining gaps</a></p><div class="stats">')
 if (ROOT/'reports/gap-audit-round-two.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="reports/gap-audit-round-two.html">Second independent audit: 12 prioritized findings</a></p><div class="stats">')
 if (ROOT/'reports/gap-closure-status.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="reports/gap-closure-status.html">Latest gap closure: acquired references, practical pathways and remaining work</a></p><div class="stats">')
 if (ROOT/'reports/us-land-data.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="reports/us-land-data.html">United States land data: map and geography downloads</a></p><div class="stats">')
 if (ROOT/'reports/survival-tools-cart.html').exists():
  doc=doc.replace('<div class="stats">','<p><a href="reports/survival-tools-cart.html">Supplies and tools: shopping research and cart status</a></p><div class="stats">')
 extra='''<script>$('encyclopedias').onclick=async()=>{if(location.protocol==='file:'){$('status').textContent='Open this library through its launcher for encyclopedia search.';return}try{$('status').textContent='Searching offline encyclopedias…';const data=await fetch('/api/collection-search?q='+encodeURIComponent($('q').value)).then(r=>r.json());if(!Array.isArray(data))throw Error(data.error||'Search error');$('status').textContent=data.length+' encyclopedia matches';$('results').innerHTML=data.map(d=>`<div class="card"><div><a class="title" href="${esc(d.path)}">${esc(d.title)}</a><div class="details">${esc(d.collection)}</div></div></div>`).join('')}catch(e){$('status').textContent=e.message}};</script>'''
 progress_script='''<script>async function progress(){if(location.protocol==='file:')return;try{const d=await fetch('/api/download-status').then(r=>r.json());let lines=[];if(d.channels){let c=d.channels.counts;lines.push(`Creator archive: ${c.complete} completed, ${c.downloading} downloading, ${c.pending} queued, ${c.unavailable} unavailable.`)}if(d.collections){for(const [name,c] of Object.entries(d.collections)){if(c.state!=='verified')lines.push(`${c.title}: ${(100*c.downloaded/c.total).toFixed(1)}% downloaded`)}}$('downloadProgress').textContent=lines.join(' ')}catch(e){}}progress();setInterval(progress,30000);</script>'''
 legacy=doc.replace('__DATA__',payload).replace('__STATS__',stats).replace('</body>',extra+progress_script+'</body>')
 app=ROOT/'app'/'index.html'
 if app.is_file():
  # Persistent app shell (app/) becomes the entry page; the generated list page is kept at /classic/.
  (ROOT/'classic').mkdir(exist_ok=True)
  atomic_write(ROOT/'classic'/'index.html',legacy.replace('<head>','<head><base href="/">',1))
  atomic_write(ROOT/'index.html',app.read_text(encoding='utf-8').replace('__BUILD__',str(int(time.time()))))
 else:atomic_write(ROOT/'index.html',legacy)

def atomic_write(path,text):
 tmp=path.with_name('.'+path.name+'.tmp');tmp.write_text(text,encoding='utf-8');os.replace(tmp,path)

ZIMS={};ZIM_LOCK=threading.Lock()
# Connections that may wait to be accepted. A page or the map asks for 20 or more files at once; with Python's default of 5,
# macOS reset the rest. server.py serves with a subclass of this.
LISTEN_QUEUE=128

class HTTPServer(http.server.ThreadingHTTPServer):
 request_queue_size=LISTEN_QUEUE

class Handler(http.server.SimpleHTTPRequestHandler):
 timeout=30  # a client that sends half a request gives its thread back; do_GET allows longer for sending
 SEND_TIMEOUT=600  # a paused video stops reading for minutes at a time; the player asks again with a Range when it resumes
 # Windows' registry can call .js text/plain, which X-Content-Type-Options: nosniff would then refuse to run.
 extensions_map={**http.server.SimpleHTTPRequestHandler.extensions_map,'.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT),**kwargs)
 def log_message(self,*args):pass
 SERVED_ONLY=False  # True: a file must really sit in a served folder, not just somewhere in the library
 def file_ok(self,path,folders=False):
  """May this translated path be read, judged by where it really leads? Links and junctions leaving the allowed places are refused."""
  try:
   if os.path.isfile(path) and os.stat(path).st_nlink>1:return False  # a hard link looks like an ordinary file at an ordinary path, so the real-path checks below cannot see where else it lives
   asked=parts_below(os.path.abspath(path),ROOT)
   if asked is None or parts_refused(asked,folders):return False
   real=real_path(path)
   for folder in SERVED_FOLDERS:
    parts=parts_below(real,real_path(ROOT/folder))
    if parts is not None:return not parts_refused((*folder.split('/'),*parts),folders)
   parts=parts_below(real,real_path(ROOT))
   if parts is None or parts_refused(parts,folders):return False
   return not self.SERVED_ONLY or parts in ((),('reader.css',))
  except (OSError,ValueError):return False
 def send_head(self):
  path=self.translate_path(self.path)
  if not self.file_ok(path,folders=True):
   self.send_error(404);return None
  if os.path.isdir(path):  # the base class would serve a folder's index file without asking
   for index in ('index.html','index.htm'):
    if os.path.exists(os.path.join(path,index)) and not self.file_ok(os.path.join(path,index),folders=True):
     self.send_error(404);return None
  return super().send_head()
 def send_data(self,data,mime='application/json',status=200):
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  self.connection.settimeout(self.SEND_TIMEOUT)
  url=urllib.parse.urlsplit(self.path)
  remote=remote_records().get(urllib.parse.unquote(url.path).lstrip('/'))
  if remote and serve_remote:
   serve_remote(self,remote);return
  if url.path=='/api/health':
   self.send_data(b'{"service":"survival-library","offline":true}');return
  if url.path=='/api/download-status':
   result={}
   for key,p in [('channels',ROOT/'channel-archive-status.json'),('collections',ROOT/'manifests/kiwix-progress.json'),('transcripts',ROOT/'transcript-status.json'),('subjects',ROOT/'subject-video-status.json')]:
    try:
     result[key]=json.loads(p.read_text(encoding='utf-8'))
     if key=='transcripts':result[key].pop('videos',None)
    except Exception:pass
   self.send_data(json.dumps(result).encode());return
  if url.path=='/api/collection-search':
   try:
    _repo=Path(__file__).resolve().parents[1]
    if str(_repo) not in sys.path:
     sys.path.insert(0,str(_repo))
    from engine import encyclopedias
    params=urllib.parse.parse_qs(url.query);q=params.get('q',[''])[0]
    # Same JSON as before: title, collection, path. Text excerpts stay inside Chat, not this UI search.
    # root=ROOT: this process may have been started with LASTBOOK_LIBRARY, which is
    # the library-data folder itself. encyclopedias.search defaults to engine.paths.LIB.
    found=encyclopedias.search(q, read_text=False, excerpt=0, root=ROOT)
    hits=[{'title':h['title'],'collection':h['collection'],'path':h['path']} for h in found]
    self.send_data(json.dumps(hits).encode())
   except CLIENT_GONE:pass
   except Exception:self.send_data(json.dumps({'error':'Encyclopedia search failed.'}).encode(),status=500)
   return
  if url.path=='/api/search':
   try:self.send_data(json.dumps(search(urllib.parse.parse_qs(url.query).get('q',[''])[0],40)).encode())
   except CLIENT_GONE:pass
   except Exception:self.send_data(json.dumps({'error':'Search failed.'}).encode(),status=500)
   return
  if url.path.startswith('/zim/'):
   try:
    from libzim.reader import Archive
    parts=url.path.split('/',3);name=urllib.parse.unquote(parts[2]);entrypath=urllib.parse.unquote(parts[3]) if len(parts)>3 else ''
    path=zim_file(name)
    with ZIM_LOCK:
     if name not in ZIMS:ZIMS[name]=Archive(path)
     arc=ZIMS[name]
    if not entrypath:
     self.send_response(302);self.send_header('Location','/zim/'+urllib.parse.quote(name)+'/'+urllib.parse.quote(zim_main_path(arc),safe='/'));self.end_headers();return
    entry=arc.get_entry_by_path(entrypath);item=entry.get_item()
    self.send_data(bytes(item.content),item.mimetype);return
   except CLIENT_GONE:return
   except Exception:self.send_data(b'Not found','text/plain; charset=utf-8',404);return  # libzim's own messages carry the full file path
  if url.path.endswith(('.html','.htm')):
   path=Path(self.translate_path(url.path))
   if path.is_file() and path.name!='index.html' and path.stat().st_size<=MAX_PARSED_PAGE and self.file_ok(path,folders=True):
    try:
     records=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
     mapping={r[k]:r['path'] for r in records for k in ['source_url','final_url'] if r.get(k)}
     soup=BeautifulSoup(path.read_text(encoding='utf-8',errors='replace'),'html.parser')
     for a in soup.select('a[href]'):
      base,fragment=urllib.parse.urldefrag(a['href'])
      if base in mapping:a['href']='/'+mapping[base]+('#'+fragment if fragment else '')
     self.send_data(str(soup).encode(),'text/html; charset=utf-8');return
    except Exception:pass
  if self.headers.get('Range') and url.path.lower().endswith(('.mp4','.webm','.mkv')):
   path=Path(self.translate_path(url.path))
   if not self.file_ok(path,folders=True):
    self.send_error(404);return
   try:
    size=path.stat().st_size;m=re.match(r'bytes=(\d+)-(\d*)',self.headers['Range'])
    if not m:raise ValueError('Bad range')
    start=int(m.group(1));end=min(int(m.group(2)) if m.group(2) else size-1,size-1)
    if start>end:raise ValueError('Bad range')
    self.send_response(206);self.send_header('Content-Type',mimetypes.guess_type(path)[0] or 'video/mp4');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Range',f'bytes {start}-{end}/{size}');self.send_header('Content-Length',str(end-start+1));self.end_headers()
    with path.open('rb') as f:
     f.seek(start);left=end-start+1
     while left:
      b=f.read(min(left,1024**2));self.wfile.write(b);left-=len(b)
   except CLIENT_GONE:pass
   except Exception:self.send_error(416)
   return
  super().do_GET()

def watch_index():
 last=None
 while True:
  files=list((ROOT/'manifests').glob('*.json'))
  if (ROOT/'ocr/status.json').exists():files.append(ROOT/'ocr/status.json')
  stamp=max((p.stat().st_mtime for p in files if not any(k in p.name for k in ['progress','failures','candidates'])),default=0)
  if stamp!=last:
   (ROOT/'logs').mkdir(exist_ok=True)
   with (ROOT/'logs/index-updater.log').open('a',encoding='utf-8') as log:
    subprocess.run([sys.executable,str(Path(__file__)),'build'],stdout=log,stderr=log)
   last=stamp
  time.sleep(120)

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
 sub.add_parser('build');p=sub.add_parser('search');p.add_argument('query');p.add_argument('--limit',type=int,default=20)
 p=sub.add_parser('serve');p.add_argument('--port',type=int,default=8879)
 args=parser.parse_args()
 if args.command=='build':
  with (ROOT/'index.lock').open('w') as lock:
   if os.name=='nt':
    import msvcrt
    while True:
     try:msvcrt.locking(lock.fileno(),msvcrt.LK_LOCK,1);break
     except OSError:pass
   else:
    import fcntl;fcntl.flock(lock,fcntl.LOCK_EX)
   build()
 elif args.command=='search':print(json.dumps(search(args.query,args.limit),indent=2,ensure_ascii=False))
 else:
  server=HTTPServer(('127.0.0.1',args.port),Handler)
  if os.environ.get('SURVIVAL_DISABLE_INDEX_WATCH')!='1':threading.Thread(target=watch_index,daemon=True).start()
  print(f'Offline library: http://127.0.0.1:{server.server_port}',flush=True);server.serve_forever()
