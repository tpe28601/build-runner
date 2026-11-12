"""Deliver key presses to a build step's console.

Console-mode prompts (``getpass``, ``msvcrt.getch``, ``ReadConsole``) wait on
raw key events in the console input buffer and never look at the stdin pipe,
so the Enter such a step waits for has to be written with ``WriteConsoleInput``
after attaching to a console in the step's process tree. Build steps start a
console app (cmd.exe) without DETACHED flags, and that console - visible or not
- is what every descendant shares and what AttachConsole reaches. (A console a
DETACHED process opens for itself through CONIN$ is not attachable; production
steps never take that path, verified by probe and by the tests.)
"""

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x2
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 0x3
KEY_EVENT = 0x1
VK_RETURN = 0x0D
SCAN_CODE_RETURN = 0x1C
INVALID = (None, 0, -1)   # c_void_p restype failure spellings

# Console host processes never wait on input themselves; attaching to them is
# at best wasted and at best shares the wrong console.
HOSTS = frozenset({"conhost.exe", "openconsole.exe", "winpty-agent.exe", "opencode.exe"})
ATTACH_PARENT_PROCESS = 0xFFFFFFFF

kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
kernel32.AttachConsole.argtypes = (wintypes.DWORD,)
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.FlushConsoleInputBuffer.argtypes = (ctypes.c_void_p,)
kernel32.FlushConsoleInputBuffer.restype = wintypes.BOOL


class ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


# Snapshot handles are 64-bit on x64: pass them as c_void_p, not truncated int.
kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W))
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W))
kernel32.Process32NextW.restype = wintypes.BOOL


class KeyEventRecord(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("UnicodeChar", ctypes.c_wchar),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class InputUnion(ctypes.Union):
    # MOUSE_EVENT_RECORD (16 bytes) is the widest member of the INPUT union,
    # so the OS reads INPUT_RECORD as exactly 20 bytes. Getting this wrong
    # silently shifts the second event of a batch into padding.
    _fields_ = [("KeyEvent", KeyEventRecord), ("Pad", ctypes.c_byte * 16)]


class Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("Union", InputUnion)]


assert ctypes.sizeof(Input) == 20, "INPUT_RECORD layout must match the console API"


kernel32.WriteConsoleInputW.argtypes = (ctypes.c_void_p, ctypes.POINTER(Input),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
kernel32.WriteConsoleInputW.restype = wintypes.BOOL


def _console_candidates(root_pid):
    """Step descendants (deepest first) that could be waiting on console input."""
    pairs = []
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in INVALID:
        return []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            pairs.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    except OSError:
        return []
    finally:
        kernel32.CloseHandle(snapshot)
    depths = {root_pid: 0}
    eligible = [root_pid]
    grown = True
    while grown:   # the snapshot order is unspecified, so expand until stable
        grown = False
        for pid, parent, exe in pairs:
            if parent in depths and pid not in depths:
                depths[pid] = depths[parent] + 1
                if exe not in HOSTS:
                    eligible.append(pid)
                grown = True
    # A grandchild reading the console must win even when the batch parent has
    # no console: deepest first; every attach lands on the one shared console.
    return sorted(eligible, key=lambda pid: -depths[pid])


def inject_enter(root_pid, char="\r"):
    """Press one key into the console input of the step's process tree.

    True when some process in the tree accepted the events. False when nothing
    in the tree owns a console (a pure-pipe step) or every attach/write failed,
    which keeps stdin-pipe prompts as the only channel, like before.
    """
    # AttachConsole fails for a caller that already has a console, so detach
    # first; the real runner is console-less and this simply returns False.
    had_console = bool(kernel32.FreeConsole())
    try:
        for pid in _console_candidates(root_pid):
            if not kernel32.AttachConsole(pid):
                continue
            opened = None
            try:
                # CONIN$ always names the console attached right now. The
                # inherited standard handle is deliberately not trusted: it can
                # still point at a redirected handle or the console detached a
                # moment earlier, where a flush would "succeed" against the
                # wrong console and the Enter would be lost with a True result.
                handle = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE,
                                              FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                              OPEN_EXISTING, 0, None)
                opened = handle
                if handle in INVALID:
                    continue
                # Drop leftovers (including our own earlier injections) so the
                # queue holds exactly this Enter. Caveat: if an operator is
                # mid-typing in the step's visible window, that input is lost.
                kernel32.FlushConsoleInputBuffer(handle)
                events = (Input * 2)()
                for index, down in enumerate((True, False)):
                    events[index].type = KEY_EVENT
                    event = events[index].Union.KeyEvent
                    event.bKeyDown = down
                    event.wRepeatCount = 1
                    event.wVirtualKeyCode = VK_RETURN
                    event.wVirtualScanCode = SCAN_CODE_RETURN
                    # A real key-up event carries no character; without this a
                    # reader taking both events would see one press as two.
                    event.UnicodeChar = char if down else "\0"
                    event.dwControlKeyState = 0
                written = wintypes.DWORD(0)
                if kernel32.WriteConsoleInputW(handle, events, 2, ctypes.byref(written)):
                    return True
            except OSError:
                continue
            finally:
                if opened is not None and opened not in INVALID:
                    kernel32.CloseHandle(opened)
                kernel32.FreeConsole()
    finally:
        if had_console:
            # Best-effort hand-back for console callers (e.g. a test run). This
            # reattaches the caller's *parent* console, which is the same one
            # only when the caller inherited it - good enough here because the
            # production runner is DETACHED and never takes this branch.
            kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
    return False
