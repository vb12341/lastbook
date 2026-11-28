"""Lastbook's Windows installer and uninstaller: one program, built twice by packaging/build_setup_windows.sh.

  Lastbook-<version>-Setup.exe  carries app.zip (the app folder, the uninstall build of this program and the icon) and installs it
                                for this user only, with no administrator prompt.
  Uninstall Lastbook.exe        the same program without app.zip. It sits in the install folder and Apps & features runs it.

Switches: /S quiet; /uninstall; /uninstall /S. Exit codes: 0 done, 1 failed, 2 Lastbook is open, 3 cancelled.

Everything goes inside this user's profile: the program in %LOCALAPPDATA%\\Programs\\Lastbook, a Start menu shortcut, an optional
desktop shortcut, and one registry key under HKCU for Apps & features. Nothing reaches the network. Uninstalling removes exactly what
was installed, as listed in installed-files.txt, and never LastbookData or Lastbook's settings.

"""
import ctypes
import os
import subprocess
import sys
import threading
import traceback
import winreg
import zipfile
from pathlib import Path

VERSION = '1.1.19'
NAME = 'Lastbook'
APP_EXE = 'Lastbook.exe'
UNINSTALLER = 'Uninstall Lastbook.exe'
ICON = 'Lastbook.ico'
MANIFEST = 'installed-files.txt'
KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Lastbook'
OK, FAILED, APP_OPEN, CANCELLED = 0, 1, 2, 3
APP_OPEN_TEXT = 'Lastbook is open. Close it, then try again.'
NO_WINDOW, DETACHED, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000008, 0x00000200, 0x01000000
POWERSHELL = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


class AppOpen(Exception):
    pass


# ---------- places ----------

class _Guid(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16), ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]


def known_folder(a, b, c, d):
    g, p = _Guid(a, b, c, (ctypes.c_ubyte * 8)(*d)), ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return Path(p.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


def install_dir():
    local = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(local) / 'Programs' / NAME


def start_menu_link():
    programs = known_folder(0xA77F5D77, 0x2E2B, 0x44C3, (0xA6, 0xA2, 0xAB, 0xA6, 0x01, 0x05, 0x4A, 0x51))    # FOLDERID_Programs
    return (programs or Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs') / 'Lastbook.lnk'


def desktop_link():
    desktop = known_folder(0xB4BFCC3A, 0xDB2C, 0x424C, (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))    # FOLDERID_Desktop
    return (desktop or Path.home() / 'Desktop') / 'Lastbook.lnk'


def payload():
    p = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'app.zip'
    return p if p.is_file() else None


def lastbook_running():
    """Asks Windows for its process list directly. Never a program by bare name: Windows would look beside Setup.exe first
    (usually Downloads), so a planted tasklist.exe there would run (audit W1)."""
    import ctypes
    from ctypes import wintypes as wt

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD), ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wt.DWORD), ('szExeFile', ctypes.c_wchar * 260)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = (wt.HANDLE, ctypes.POINTER(Entry))
    k.CloseHandle.argtypes = (wt.HANDLE,)
    snap = k.CreateToolhelp32Snapshot(2, 0)     # TH32CS_SNAPPROCESS
    if snap in (None, wt.HANDLE(-1).value):
        return False
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() == APP_EXE.lower():
                return True
            ok = k.Process32NextW(snap, ctypes.byref(e))
        return False
    finally:
        k.CloseHandle(snap)


def powershell(script, **env):
    """Runs a few lines of Windows PowerShell with the values passed as environment variables, so no path is ever quoted into code."""
    e = dict(os.environ, **{f'LB_{k}': str(v) for k, v in env.items()})
    subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-Command', script], env=e, check=True, capture_output=True,
                   creationflags=NO_WINDOW, cwd=str(Path(os.environ.get('SystemRoot', r'C:\Windows'))))


LINK_SCRIPT = ('$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LB_LINK); $s.TargetPath = $env:LB_TARGET; '
               '$s.WorkingDirectory = $env:LB_DIR; $s.IconLocation = $env:LB_ICON + ",0"; $s.Description = "Lastbook"; $s.Save()')


def make_link(link, d):
    link.parent.mkdir(parents=True, exist_ok=True)
    powershell(LINK_SCRIPT, LINK=link, TARGET=d / APP_EXE, DIR=d, ICON=d / ICON)


# ---------- install and remove ----------

def read_manifest(d):
    try:
        lines = (d / MANIFEST).read_text(encoding='utf-8').splitlines()
    except OSError:
        return [], []
    files = [l for l in lines if l and not l.startswith('link\t')]
    links = [Path(l.split('\t', 1)[1]) for l in lines if l.startswith('link\t')]
    return files, links


def remove_installed(d, keep=None):
    """Removes what the manifest in d lists, then folders left empty. Anything else in d stays. The manifest is only a list:
    a name outside the install folder, or a link other than the two this installer makes, is skipped (audit W3)."""
    files, links = read_manifest(d)
    ours = {str(start_menu_link()).lower(), str(desktop_link()).lower()}
    for link in links:
        if str(link).lower() in ours and link.suffix.lower() == '.lnk':
            link.unlink(missing_ok=True)
    root = d.resolve()
    for name in [n for n in files if safe_name(n)] + [MANIFEST]:
        p = d / name
        try:
            inside = p.resolve().is_relative_to(root) and p.resolve() != root
        except OSError:
            continue
        if not inside or (keep and p.resolve() == keep):
            continue
        p.unlink(missing_ok=True)
    if d.is_dir():
        for sub in sorted((p for p in d.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                sub.rmdir()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def safe_name(name):
    """A plain relative name inside the install folder, judged as a Windows path: no drive, no leading slash, no '..', no ':'."""
    from pathlib import PureWindowsPath
    p = PureWindowsPath(name)
    return (bool(name) and not p.drive and not p.root and not name.startswith(('/', '\\')) and '..' not in p.parts
            and ':' not in name and name != MANIFEST)


def install(desktop, progress=lambda f: None):
    d = install_dir()
    if lastbook_running():
        raise AppOpen()
    with zipfile.ZipFile(payload()) as z:
        items = [i for i in z.infolist() if not i.is_dir()]
        if not all(safe_name(i.filename) for i in items) or APP_EXE not in {i.filename for i in items}:
            raise RuntimeError('This installer is damaged. Download it again.')
        remove_installed(d)
        unregister()
        try:
            links = [start_menu_link()] + ([desktop_link()] if desktop else [])
            d.mkdir(parents=True, exist_ok=True)
            (d / MANIFEST).write_text(''.join(f'{i.filename}\n' for i in items) + ''.join(f'link\t{l}\n' for l in links), encoding='utf-8')
            total, done = sum(i.file_size for i in items) or 1, 0
            for i in items:
                target = d / i.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(target, 'wb') as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
                        done += len(chunk)
                        progress(0.95 * done / total)
            for link in links:
                make_link(link, d)
            progress(0.98)
            register(d, sum(i.file_size for i in items))
            progress(1.0)
        except BaseException:
            remove_installed(d)
            unregister()
            raise
    return d


def register(d, size):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_WRITE) as k:
        for name, value in (('DisplayName', NAME), ('DisplayVersion', VERSION), ('Publisher', NAME), ('DisplayIcon', str(d / ICON)),
                            ('InstallLocation', str(d)), ('UninstallString', f'"{d / UNINSTALLER}" /uninstall'),
                            ('QuietUninstallString', f'"{d / UNINSTALLER}" /uninstall /S')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name, value in (('EstimatedSize', (size + 1023) // 1024), ('NoModify', 1), ('NoRepair', 1)):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)


def unregister():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
    except FileNotFoundError:
        pass


REMOVE_LATER = r'''try { Wait-Process -Id ([int]$env:LB_WAIT) -Timeout 120 -ErrorAction Stop } catch {}
for ($i = 0; $i -lt 40; $i++) { Remove-Item -LiteralPath $env:LB_FILE -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path -LiteralPath $env:LB_FILE)) { break }; Start-Sleep -Milliseconds 250 }
if ((Test-Path -LiteralPath $env:LB_DIR) -and -not (Get-ChildItem -LiteralPath $env:LB_DIR -Force)) { Remove-Item -LiteralPath $env:LB_DIR -Force }'''


def uninstall():
    d = install_dir()
    if lastbook_running():
        raise AppOpen()
    me = Path(sys.executable).resolve() if getattr(sys, 'frozen', False) else None
    inside = me is not None and me.parent == d.resolve()
    unregister()
    remove_installed(d, keep=me if inside else None)
    start_menu_link().unlink(missing_ok=True)    # also when the manifest was lost
    if inside:    # a running program cannot delete itself: a hidden PowerShell does it once this one has exited
        e = dict(os.environ, LB_WAIT=str(os.getppid()), LB_FILE=str(me), LB_DIR=str(d))
        args = [str(POWERSHELL), '-NoProfile', '-NonInteractive', '-Command', REMOVE_LATER]
        for flags in (NO_WINDOW | NEW_GROUP | BREAKAWAY, NO_WINDOW | NEW_GROUP):    # not DETACHED_PROCESS: PowerShell then exits at once, doing nothing
            try:
                subprocess.Popen(args, env=e, creationflags=flags, cwd=str(d.parent.parent), close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except OSError:
                continue


def open_lastbook(d):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('_PYI', '_MEI'))}
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'    # Lastbook.exe is a PyInstaller program too: it must not take this one's settings
    subprocess.Popen([str(d / APP_EXE)], cwd=str(d), env=env, creationflags=DETACHED | NEW_GROUP, close_fds=True)


# ---------- the window ----------

def light_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return bool(winreg.QueryValueEx(k, 'AppsUseLightTheme')[0])
    except OSError:
        return True


def net():
    import clr
    clr.AddReference('System.Windows.Forms')
    clr.AddReference('System.Drawing')
    import System
    import System.Drawing as D
    import System.Threading as T
    import System.Windows.Forms as F
    return System, D, T, F


def dark_caption(hwnd, light):
    v = ctypes.c_int(0 if light else 1)
    ctypes.WinDLL('dwmapi', winmode=0x800).DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(v), ctypes.sizeof(v))   # DWMWA_USE_IMMERSIVE_DARK_MODE


def system_dpi():
    try:
        return ctypes.windll.user32.GetDpiForSystem() or 96
    except AttributeError:    # before Windows 10 1607
        return 96


def run_window():
    k = system_dpi() / 96    # the layout below is in 96-DPI pixels; main() made this process DPI aware, so it is scaled here. Fonts, in points, scale by themselves.
    S = lambda v: int(round(v * k))
    System, D, T, F = net()
    light = light_theme()
    C = lambda h: D.ColorTranslator.FromHtml(h)
    body, foot = (C('#ffffff'), C('#f3f3f3')) if light else (C('#2b2b2b'), C('#202020'))
    ink, quiet = (C('#1b1b1b'), C('#5f5f5f')) if light else (C('#ffffff'), C('#c5c5c5'))
    accent, accent_ink = (C('#10805c'), C('#ffffff')) if light else (C('#3cc690'), C('#06170f'))
    plain, line = (C('#fbfbfb'), C('#d1d1d1')) if light else (C('#373737'), C('#454545'))
    track = C('#e0e0e0') if light else C('#454545')
    result, failure = [CANCELLED], []

    def build():
        F.Application.EnableVisualStyles()
        F.Application.SetCompatibleTextRenderingDefault(False)
        font = D.Font('Segoe UI', 9.0)
        form = F.Form()
        form.Text, form.Font, form.BackColor, form.ForeColor = 'Lastbook Setup', font, body, ink
        form.FormBorderStyle, form.MaximizeBox, form.MinimizeBox = F.FormBorderStyle.FixedSingle, False, True
        form.StartPosition, form.ClientSize = F.FormStartPosition.CenterScreen, D.Size(S(500), S(300))
        form.Icon = D.Icon.ExtractAssociatedIcon(sys.executable)

        def add(ctrl, x, y, w, h, parent=form):
            ctrl.Location, ctrl.Size = D.Point(S(x), S(y)), D.Size(S(w), S(h))
            parent.Controls.Add(ctrl)
            return ctrl

        def label(text, x, y, w, h, size=9.0, bold=False, colour=None):
            l = F.Label()
            l.Text, l.AutoEllipsis, l.ForeColor = text, True, colour or ink
            l.Font = D.Font('Segoe UI Semibold' if bold else 'Segoe UI', size)
            return add(l, x, y, w, h)

        mark = F.PictureBox()
        mark.Image, mark.SizeMode = form.Icon.ToBitmap(), F.PictureBoxSizeMode.Zoom
        add(mark, 28, 28, 48, 48)
        title = label('Install Lastbook', 92, 26, 380, 32, 15.0, True)
        sub = label(f'Version {VERSION}', 94, 58, 380, 20, colour=quiet)
        where_head = label('Install folder', 28, 100, 444, 18, colour=quiet)
        where = label(str(install_dir()), 28, 120, 444, 20)
        desk = F.CheckBox()
        desk.Text, desk.Checked, desk.ForeColor = 'Add a desktop shortcut', desktop_link().exists(), ink    # an update keeps the shortcut the person has
        add(desk, 28, 152, 300, 24)
        bar = F.Panel()     # drawn in the window's own colours: the system progress bar stays light on a dark window
        bar.BackColor, bar.Visible = track, False
        add(bar, 28, 200, 444, 6)
        fill = F.Panel()
        fill.BackColor = accent
        add(fill, 0, 0, 0, 6, bar)

        footer = F.Panel()
        footer.BackColor = foot
        add(footer, 0, 228, 500, 72)

        def button(text, x, primary):
            b = F.Button()
            b.Text, b.FlatStyle, b.Font = text, F.FlatStyle.Flat, font
            b.BackColor, b.ForeColor = (accent, accent_ink) if primary else (plain, ink)
            b.FlatAppearance.BorderColor = accent if primary else line
            b.FlatAppearance.BorderSize = 1
            b.UseVisualStyleBackColor = False
            return add(b, x, 20, 140, 32, footer)

        go = button('Install', 184, True)
        stop = button('Cancel', 332, False)
        form.AcceptButton, form.CancelButton = go, stop
        state = {'busy': False, 'done': False, 'progress': 0.0, 'outcome': None}

        def work():
            try:
                state['outcome'] = ('ok', install(desk_checked, lambda f: state.__setitem__('progress', f)))
            except AppOpen:
                state['outcome'] = ('open', None)
            except Exception as e:
                traceback.print_exc()
                state['outcome'] = ('error', str(e) or e.__class__.__name__)

        timer = F.Timer()
        timer.Interval = 50

        def tick(sender, args):
            fill.Width = S(444 * state['progress'])
            outcome = state['outcome']
            if outcome is None:
                return
            timer.Stop()
            state['busy'], state['outcome'] = False, None
            go.Enabled = stop.Enabled = desk.AutoCheck = True
            if outcome[0] == 'ok':
                state['done'], result[0] = True, OK
                title.Text, sub.Text = 'Lastbook is installed', 'Find it in the Start menu.'
                for c in (where_head, where, desk, bar):
                    c.Visible = False
                go.Text, stop.Text = 'Open Lastbook', 'Close'
                go.Focus()
                return
            bar.Visible = False
            text = APP_OPEN_TEXT if outcome[0] == 'open' else f'Lastbook could not be installed.\n\n{outcome[1]}'
            result[0] = APP_OPEN if outcome[0] == 'open' else FAILED
            F.MessageBox.Show(form, text, 'Lastbook Setup', F.MessageBoxButtons.OK,
                              F.MessageBoxIcon.Information if outcome[0] == 'open' else F.MessageBoxIcon.Error)

        timer.Tick += tick
        desk_checked = False

        def on_go(sender, args):
            nonlocal desk_checked
            if state['done']:
                try:
                    open_lastbook(install_dir())
                except Exception as e:
                    F.MessageBox.Show(form, f'Lastbook could not be opened.\n\n{e}', 'Lastbook Setup')
                form.Close()
                return
            desk_checked = bool(desk.Checked)
            state['busy'], state['progress'] = True, 0.0
            go.Enabled = stop.Enabled = desk.AutoCheck = False    # a disabled box goes grey on grey in dark mode, so it only stops changing
            fill.Width, bar.Visible = 0, True
            timer.Start()
            threading.Thread(target=work, daemon=True).start()

        def on_stop(sender, args):
            form.Close()

        def on_closing(sender, args):
            if state['busy']:
                args.Cancel = True

        def on_shown(sender, args):
            dark_caption(form.Handle.ToInt64(), light)

        go.Click += on_go
        stop.Click += on_stop
        form.FormClosing += on_closing
        form.HandleCreated += on_shown
        F.Application.Run(form)

    def body_safe():
        try:
            build()
        except Exception as e:    # an exception must not cross into .NET
            traceback.print_exc()
            failure.append(e)

    thread = T.Thread(T.ThreadStart(body_safe))
    thread.SetApartmentState(T.ApartmentState.STA)
    thread.Start()
    thread.Join()
    if failure:
        message(f'Lastbook Setup could not start.\n\n{failure[0]}', 0x10)
        return FAILED
    return result[0]


def message(text, flags):
    return ctypes.windll.user32.MessageBoxW(None, text, 'Lastbook', flags)


def main(argv):
    ctypes.windll.user32.SetProcessDPIAware()    # first: the uninstaller's message boxes would otherwise be stretched and blurry
    args = {a.lower() for a in argv}
    quiet = '/s' in args
    if '/uninstall' in args or payload() is None:
        if not quiet and message('Remove Lastbook from this computer?\n\nYour library (LastbookData) and settings stay.', 0x24) != 6:   # MB_YESNO | MB_ICONQUESTION, IDYES
            return CANCELLED
        try:
            uninstall()
        except AppOpen:
            quiet or message(APP_OPEN_TEXT, 0x40)
            return APP_OPEN
        except Exception as e:
            traceback.print_exc()
            quiet or message(f'Lastbook could not be removed.\n\n{e}', 0x10)
            return FAILED
        quiet or message('Lastbook is removed.', 0x40)
        return OK
    if quiet:
        try:
            install('/desktop' in args)
        except AppOpen:
            return APP_OPEN
        except Exception:
            traceback.print_exc()
            return FAILED
        return OK
    return run_window()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
