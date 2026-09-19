"""Windows mouse pass-through and a thread-owned global hotkey (no input hooks)."""
import ctypes as c
from ctypes import wintypes as w

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
from PySide6.QtWidgets import QApplication

USER32 = c.WinDLL('user32', use_last_error=True)
USER32.GetWindowLongW.argtypes = [w.HWND, c.c_int]
USER32.GetWindowLongW.restype = w.LONG
USER32.SetWindowLongW.argtypes = [w.HWND, c.c_int, w.LONG]
USER32.SetWindowLongW.restype = w.LONG
USER32.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
USER32.SetWindowPos.restype = w.BOOL
USER32.RegisterHotKey.argtypes = [w.HWND, c.c_int, w.UINT, w.UINT]
USER32.RegisterHotKey.restype = w.BOOL
USER32.UnregisterHotKey.argtypes = [w.HWND, c.c_int]
USER32.UnregisterHotKey.restype = w.BOOL

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_NOACTIVATE = 0x8000000
WS_EX_TOPMOST = 0x8


def set_input_mode(hwnd, passthrough, *, topmost=True):
    """Layered + transparent passes input to other processes; never activate on toggle."""
    hwnd = int(hwnd)
    style = USER32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if passthrough:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    else:
        # Retain Qt's layered rendering and remove only the input-related flags we own.
        style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    c.set_last_error(0)
    previous = USER32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    if not previous and c.get_last_error():
        raise c.WinError(c.get_last_error())
    if not USER32.SetWindowPos(hwnd, w.HWND(-1 if topmost else -2), 0, 0, 0, 0,
                              0x0001 | 0x0002 | 0x0010 | 0x0020):
        raise c.WinError(c.get_last_error())


class GlobalModeHotkey(QAbstractNativeEventFilter):
    # A thread-owned registration survives HWND recreation caused by Qt flags changes.
    IDENTIFIER = 0x4A52

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.registered = False
        self.label = ''
        self.modifiers = 0
        self.last_error = 0
        self.installed = False

    def register(self):
        if self.registered:
            return True
        for modifiers, label in ((0x0003, 'Ctrl + Alt + F10'),
                                  (0x0007, 'Ctrl + Alt + Shift + F10')):
            if USER32.RegisterHotKey(None, self.IDENTIFIER, modifiers | 0x4000, 0x79):
                self.registered = True
                self.modifiers, self.label = modifiers, label
                QApplication.instance().installNativeEventFilter(self)
                self.installed = True
                return True
            self.last_error = c.get_last_error()
        self.label = '快捷键被占用，请使用托盘菜单'
        return False

    def nativeEventFilter(self, event_type, message):
        if self.registered and bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            msg = w.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == self.IDENTIFIER:
                QTimer.singleShot(0, self.callback)
                return True, 0
        return False, 0

    def close(self):
        if self.registered:
            USER32.UnregisterHotKey(None, self.IDENTIFIER)
            self.registered = False
        if self.installed:
            QApplication.instance().removeNativeEventFilter(self)
            self.installed = False
