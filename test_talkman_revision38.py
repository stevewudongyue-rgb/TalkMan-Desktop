"""UIA 读取到 Desktop 红框的整条坐标链路；只移动专门创建的测试窗口。"""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import talkman_windows_coordinates as coordinates
from talkman_target import TargetError
from talkman_target_windows import WindowsTargetBackend


class CoordinateTests(unittest.TestCase):
    def call_with_context(self, switch, operation):
        with patch.object(coordinates, 'sys', SimpleNamespace(platform='win32')), patch.object(
                coordinates.ctypes, 'WinDLL',
                return_value=SimpleNamespace(SetThreadDpiAwarenessContext=switch), create=True):
            return operation()

    def test_read_uses_physical_context_and_restores_original(self):
        switch = Mock(side_effect=[1234, 1])
        test = self
        class Field:
            @property
            def CurrentBoundingRectangle(self):
                test.assertEqual(switch.call_count, 1)
                test.assertEqual(switch.call_args.args[0].value, C.c_void_p(-4).value)
                return SimpleNamespace(left=-420, top=80, right=-100, bottom=160)
        b = WindowsTargetBackend()
        rect = self.call_with_context(switch, lambda: b._position_rect(Field()))
        self.assertEqual(rect, (-420, 80, 320, 80))
        switch.assert_called_with(1234)
        self.assertIs(switch.restype, C.c_void_p)

    def test_failed_context_never_reads_unverified_rectangle(self):
        switch = Mock(return_value=0)
        b = WindowsTargetBackend()
        b._current = Mock()
        with self.assertRaisesRegex(TargetError, '屏幕缩放'):
            self.call_with_context(switch, lambda: b._position_rect(object()))
        b._current.assert_not_called()
        self.assertEqual(switch.call_count, 1)

    def test_disappeared_control_restores_original_context(self):
        switch = Mock(side_effect=[5678, 1])
        class Field:
            @property
            def CurrentBoundingRectangle(self):
                raise OSError('控件已消失')
        with self.assertRaisesRegex(TargetError, '没有可见的位置'):
            self.call_with_context(switch, lambda: WindowsTargetBackend()._position_rect(Field()))
        switch.assert_called_with(5678)

    def test_exception_inside_context_restores_and_propagates(self):
        switch = Mock(side_effect=[5678, 1])
        def operation():
            with coordinates.physical_screen_coordinates():
                raise RuntimeError('绘制失败')
        with self.assertRaisesRegex(RuntimeError, '绘制失败'):
            self.call_with_context(switch, operation)
        switch.assert_called_with(5678)

    def test_other_platform_never_calls_windows_api(self):
        with patch.object(coordinates, 'sys', SimpleNamespace(platform='darwin')), patch.object(
                coordinates.ctypes, 'WinDLL', create=True) as win:
            rect = WindowsTargetBackend()._position_rect(SimpleNamespace(CurrentBoundingRectangle=(10, 20, 50, 60)))
        self.assertEqual(rect, (10, 20, 50, 60))
        win.assert_not_called()


def native_fixture(path):
    u = C.WinDLL('user32')
    u.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]
    u.SetThreadDpiAwarenessContext.restype = C.c_void_p
    u.SetThreadDpiAwarenessContext(C.c_void_p(-4))
    create = u.CreateWindowExW
    create.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int,
                       C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
    create.restype = W.HWND
    root = create(0x08000000, 'STATIC', 'TalkMan 坐标隔离验收', 0x10CF0000,
                  80, 80, 480, 320, None, None, None, None)
    fields = [create(0x200, 'EDIT', '', 0x50010004, 20, 40+i*100, 420, 70, root,
                     100+i, None, None) for i in range(2)]
    assert root and all(fields)
    Path(path).write_text(json.dumps(dict(root=root, fields=fields)), encoding='utf-8')
    u.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    u.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
    u.DispatchMessageW.restype = C.c_ssize_t
    msg = W.MSG()
    while u.GetMessageW(C.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(C.byref(msg))
        u.DispatchMessageW(C.byref(msg))


@unittest.skipUnless(sys.platform == 'win32', '需要 Windows 原生 UIA 和屏幕坐标')
class NativeCoordinateTests(unittest.TestCase):
    def test_uia_to_desktop_red_outline_all_monitors_and_dpi_modes(self):
        import tkinter as tk
        from talkman_position_picker import PositionPicker
        from talkman_target_windows import TargetWindow, _process_path
        u = C.WinDLL('user32')
        u.SetThreadDpiAwarenessContext.argtypes = [C.c_void_p]
        u.SetThreadDpiAwarenessContext.restype = C.c_void_p
        u.GetThreadDpiAwarenessContext.restype = C.c_void_p
        u.AreDpiAwarenessContextsEqual.argtypes = [C.c_void_p, C.c_void_p]
        u.AreDpiAwarenessContextsEqual.restype = W.BOOL
        u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        u.GetWindowRect.restype = W.BOOL
        u.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        u.SetWindowPos.restype = W.BOOL
        u.GetAncestor.argtypes = [W.HWND, W.UINT]
        u.GetAncestor.restype = W.HWND
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
        u.EnumDisplayMonitors.argtypes = [W.HDC, C.POINTER(W.RECT), callback_type, W.LPARAM]
        monitors = []
        @callback_type
        def monitor(handle, dc, rect, data):
            monitors.append((rect.contents.left, rect.contents.top))
            return True
        with coordinates.physical_screen_coordinates():
            self.assertTrue(u.EnumDisplayMonitors(None, None, monitor, 0))
        def physical_rect(hwnd):
            r = W.RECT()
            with coordinates.physical_screen_coordinates():
                self.assertTrue(u.GetWindowRect(hwnd, C.byref(r)))
            return (r.left, r.top, r.right-r.left, r.bottom-r.top)
        root = tk.Tk()
        root.withdraw()
        root.fonts = SimpleNamespace(ui='Arial')
        picker = PositionPicker(root)
        b = WindowsTargetBackend()
        try:
            with tempfile.TemporaryDirectory(prefix='TalkMan-coordinates-') as folder:
                state = Path(folder)/'window.json'
                process = subprocess.Popen([sys.executable, __file__, '--fixture', str(state)],
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    deadline = time.monotonic()+10
                    while not state.exists() and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(state.exists(), '测试窗口未启动')
                    data = json.loads(state.read_text('utf-8'))
                    target = TargetWindow(data['root'], process.pid, '测试', 'TalkMan 坐标隔离验收', _process_path(process.pid))
                    for mx, my in monitors:
                        with coordinates.physical_screen_coordinates():
                            self.assertTrue(u.SetWindowPos(target.id, None, mx+60, my+80, 480, 320, 0x0014))
                        time.sleep(.08)
                        for mode in (-1, -2, -4):
                            old = u.SetThreadDpiAwarenessContext(C.c_void_p(mode))
                            self.assertTrue(old)
                            try:
                                entries = b.list_input_positions(target, activate=False)
                                self.assertEqual(len(entries), 2)
                                for number, entry in enumerate(entries, 1):
                                    rect = b.validate_input_position(target, entry)
                                    self.assertEqual(rect, physical_rect(data['fields'][number-1]))
                                    picker.show_marks([dict(index=number-1, rect=rect)])
                                    root.update_idletasks()
                                    x, y, w, h = rect
                                    label_y = y-32 if y >= 32 else y
                                    boxes = [(x,y,w,h), (x,y,w,5), (x,y+h-5,w,5),
                                             (x,y,5,h), (x+w-5,y,5,h), (x,label_y,112,32)]
                                    self.assertEqual(len(picker.marks), 6)
                                    for mark, box in zip(picker.marks, boxes):
                                        self.assertEqual(physical_rect(u.GetAncestor(mark.winfo_id(), 2)), box)
                                    self.assertTrue(u.AreDpiAwarenessContextsEqual(
                                        u.GetThreadDpiAwarenessContext(), C.c_void_p(mode)))
                            finally:
                                picker.clear_marks()
                                u.SetThreadDpiAwarenessContext(old)
                    print(f'原生验证：{len(monitors)} 个显示器 × 3 种调用线程缩放 × 2 个 EDIT；读取、复核、四边红框及编号全部对齐。', flush=True)
                finally:
                    process.terminate()
                    process.wait(timeout=10)
        finally:
            picker.clear_marks()
            root.destroy()
            b.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture':
        native_fixture(sys.argv[2])
    else:
        unittest.main()
