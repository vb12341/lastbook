# PyInstaller spec for Lastbook.app (arm64). Run through packaging/build_app_macos.sh.
# The product is app/main.py plus what it imports. app/tests is never on the search path.
import importlib
import os
import re
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
APP = ROOT / 'app'


SHIP = Path(os.environ['LASTBOOK_SHIP'])   # web/ and content/ as packaged: written by ship_app_files.py, never the source folders


def files(folder, dest, pattern='*'):
    return [(str(p), dest) for p in sorted((SHIP / folder).glob(pattern)) if p.is_file() and p.name != '.DS_Store']


def clean_sysconfig():
    """The standard library's record of how Python was built names folders on the build machine, and nothing in the app reads them.
    Written to a folder searched first, so this copy goes in instead."""
    name = sysconfig._get_sysconfigdata_name()
    built = importlib.import_module(name).build_time_vars
    clean = {k: re.sub(r'/Users/[^\s:\'"]+', '/python', v) if isinstance(v, str) else v for k, v in built.items()}
    folder = Path(workpath) / 'clean-sysconfig'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{name}.py').write_text(f'build_time_vars = {clean!r}\n', encoding='utf-8')
    return folder


datas = files('web', 'web') + files('content', 'content', 'gaps.json') + files('content/topics', 'content/topics', '*.json')
binaries = []
hidden = ['server', 'retrieval', 'assistant', 'llm_local', 'engine.paths', 'engine.library', 'engine.safe_paths', 'engine.encyclopedias', 'engine.saved_pages', 'engine.address_search', 'engine.page_tables', 'engine.notes', 'engine.model_server', 'bridge', 'shell_macos', 'PyObjCTools.AppHelper', 'update_macos']
# The optional local AI's runner (packaging/llm/build_llm_macos.sh), in Contents/Frameworks/llm/. The model itself never ships.
LLM = ROOT / 'derived' / 'llm' / 'bin-mac'
binaries += [(str(LLM / 'lastbook-llm'), 'llm')]
datas += [(str(LLM / 'LICENSE-llama.cpp.txt'), 'llm')]
datas += [(str(SHIP / 'setup' / 'manifest-v1.json'), 'setup')]   # what first-run setup can install
hidden += collect_submodules('engine.setup')
datas += [(str(ROOT / 'engine' / 'setup' / 'roots.pem'), 'engine/setup')]   # the only roots setup's downloader trusts
for package in ('libzim', 'pypdfium2', 'pypdfium2_raw'):   # loaded only when a ZIM or PDF is first opened, and they carry native libraries
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h
for package in ('objc', 'CoreFoundation', 'Foundation', 'AppKit', 'WebKit'):   # includes each framework's metadata, without which calls into WebKit fail
    hidden += collect_submodules(package)

EXCLUDES = [
    'remote_storage',                                    # stays in the library's own tools: no machine names or ssh code in the package
    'shell_windows', 'clr', 'clr_loader', 'pythonnet',   # the Windows window
    'webview', 'bottle', 'proxy_tools',                  # the old window
    'onnxruntime', 'tokenizers',                         # see requirements_macos.txt
    'numpy._core._multiarray_tests', 'numpy._core._umath_tests', 'numpy._core._operand_flag_tests', 'numpy._core._rational_tests', 'numpy._core._struct_ufunc_tests',
    'PyObjCTest', 'pytest', 'unittest', 'doctest', 'test', 'numpy.testing', 'numpy.tests', 'numpy.f2py', 'pypdfium2_cli',
    'tkinter', 'idlelib', 'lib2to3', 'ensurepip', 'venv', 'setuptools', 'pkg_resources', 'pip', 'PyInstaller',
]

a = Analysis([str(APP / 'main.py')], pathex=[str(clean_sysconfig()), str(APP), str(ROOT)], binaries=binaries, datas=datas, hiddenimports=hidden, excludes=EXCLUDES, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')], exclude_binaries=True, name='Lastbook', console=False, argv_emulation=False, target_arch='arm64', codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name='Lastbook')
# Update now (app/update_macos.py): only when build_app_macos.sh verified a pinned Sparkle.framework and was given the public key.
# Without both, the app has no SU* keys and About offers the download page. The key is public; the private one never comes here.
SPARKLE_KEY = os.environ.get('LASTBOOK_SPARKLE_PUBLIC_ED_KEY', '')
SPARKLE = {}
if SPARKLE_KEY:
    import sys
    sys.path.insert(0, str(APP))
    import update_macos
    SPARKLE = {k: v for k, v in update_macos.REQUIRED.items() if k != 'CFBundleIdentifier'} | {'SUPublicEDKey': SPARKLE_KEY}
    if not update_macos.public_key_ok(SPARKLE_KEY):
        raise SystemExit('LASTBOOK_SPARKLE_PUBLIC_ED_KEY is not a base64 Ed25519 public key')
ICON = Path(SPECPATH) / 'Lastbook.icns'
app = BUNDLE(coll, name='Lastbook.app', icon=str(ICON) if ICON.exists() else None, bundle_identifier='local.lastbook.desktop', version='1.1.19', info_plist={
    'CFBundleDisplayName': 'Lastbook',
    'CFBundleName': 'Lastbook',
    'LSApplicationCategoryType': 'public.app-category.reference',
    'LSMinimumSystemVersion': '14.0',   # what numpy's binaries ask for
    'LSRequiresNativeExecution': True,
    'NSHighResolutionCapable': True,
    # Lastbook looks for its library (LastbookData) in these folders; macOS shows these words when it asks to let it look.
    'NSDocumentsFolderUsageDescription': 'Lastbook looks here for its library, the LastbookData folder. It reads and saves only inside that folder.',
    'NSDownloadsFolderUsageDescription': 'Lastbook looks here for its library, the LastbookData folder. It reads and saves only inside that folder.',
    'NSDesktopFolderUsageDescription': 'Lastbook looks here for its library, the LastbookData folder. It reads and saves only inside that folder.',
} | SPARKLE)
