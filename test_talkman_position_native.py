"""独立 Win32 测试窗口，两块真实 EDIT；不向任何用户 Agent 写入。"""
import ctypes
from ctypes import wintypes as wt
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest


def fixture(path):
    user = ctypes.windll.user32
    create = user.CreateWindowExW
    create.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    create.restype = wt.HWND
    root = create(0, 'STATIC', 'TalkMan 输入位置隔离验收', 0x10CF0000,
                  100, 100, 480, 320, None, None, None, None)
    fields = [create(0x200, 'EDIT', value, 0x50010004, 20, 40+index*100, 420, 75,
                     root, index+100, None, None)
              for index, value in enumerate(('主框原文', '回答原文'))]
    assert root and all(fields)
    Path(path).write_text(json.dumps(dict(root=root, fields=fields)), encoding='utf-8')
    msg = wt.MSG()
    while user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user.TranslateMessage(ctypes.byref(msg))
        user.DispatchMessageW(ctypes.byref(msg))


@unittest.skipUnless(sys.platform == 'win32', '需要 Windows 原生 EDIT 控件')
class WindowsNativePositionTests(unittest.TestCase):
    def test_choose_second_fill_and_sync_without_touching_first(self):
        from talkman_target import TargetController, TargetWindow
        from talkman_target_windows import WindowsTargetBackend, _process_path
        with tempfile.TemporaryDirectory(prefix='TalkMan-native-position-') as folder:
            state = Path(folder)/'window.json'
            process = subprocess.Popen([sys.executable, __file__, '--fixture', str(state)],
                creationflags=subprocess.CREATE_NO_WINDOW)
            controller = None
            try:
                deadline = time.monotonic()+10
                while not state.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(state.exists(), '测试窗口未启动')
                data = json.loads(state.read_text(encoding='utf-8'))
                controller = TargetController(Path(folder), backend=WindowsTargetBackend())
                target = TargetWindow(data['root'], process.pid, '测试工具', 'TalkMan 输入位置隔离验收',
                                      _process_path(process.pid).casefold())
                controller.select(target)
                replies = queue.Queue()
                callback = lambda status, value: replies.put((status, value))
                def result(expected):
                    status, value = replies.get(timeout=15)
                    self.assertEqual(status, expected, str(value))
                    return value
                controller.begin_positions(callback)
                positions = result('ok')
                self.assertEqual(positions['count'], 2)
                controller.position_action('next', callback)
                self.assertEqual(result('ok')['index'], 1)
                controller.position_action('confirm', callback)
                result('confirmed')
                controller.fill_with_receipt('只填回答框', callback)
                receipt = result('ok')
                self.assertFalse(receipt.send_token, '普通 EDIT 不应猜测回车发送')
                send = ctypes.windll.user32.SendMessageW
                send.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
                send.restype = wt.LPARAM
                def read(hwnd):
                    buf = ctypes.create_unicode_buffer(1024)
                    send(hwnd, 0x000D, 1024, ctypes.addressof(buf))
                    return buf.value
                self.assertEqual(read(data['fields'][0]), '主框原文')
                self.assertEqual(read(data['fields'][1]), '回答原文只填回答框')
                controller.sync('同步测试', callback)
                result('ok')
                self.assertEqual(read(data['fields'][0]), '主框原文')
                self.assertEqual(read(data['fields'][1]), '回答原文只填回答框同步测试')
                controller.sync('同步改文', callback)
                result('ok')
                self.assertEqual(read(data['fields'][1]), '回答原文只填回答框同步改文')
                ctypes.windll.user32.ShowWindow(wt.HWND(data['fields'][1]), 0)
                controller.fill_with_receipt('不应回退', callback)
                result('error')
                self.assertEqual(read(data['fields'][0]), '主框原文')
            finally:
                if controller:
                    controller.close()
                process.terminate()
                process.wait(timeout=10)


@unittest.skipUnless(sys.platform == 'darwin', '需要 Mac 原生 AXValue')
class MacNativeRectangleTests(unittest.TestCase):
    def test_real_axvalue_decodes_negative_origin_and_size(self):
        import ApplicationServices as AX
        from talkman_target_macos import _NativeAPI
        position = AX.AXValueCreate(AX.kAXValueCGPointType, (-240.0, 180.0))
        size = AX.AXValueCreate(AX.kAXValueCGSizeType, (360.0, 120.0))
        api = _NativeAPI.__new__(_NativeAPI)
        api.ax = AX
        self.assertEqual(api.rect_values(position, size), (-240.0, 180.0, 360.0, 120.0))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture':
        fixture(sys.argv[2])
    else:
        unittest.main()
