"""Windows API 指针声明与剪贴板内存管理的离线测试。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import talkman_target_windows as target


class FakeFunction:
    def __init__(self, result=1):
        self.argtypes = None
        self.restype = None
        self.result = result

    def __call__(self, *args):
        return self.result


class FakeLibrary:
    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, FakeFunction())


class WindowsAPITests(unittest.TestCase):
    def test_user32_declares_pointer_sized_handles(self):
        library = FakeLibrary()
        with patch.object(target.os, "name", "nt"), patch.object(
            target.ctypes, "windll", SimpleNamespace(user32=library), create=True
        ):
            self.assertIs(target._win_api(), library)
        self.assertIs(library.GetForegroundWindow.restype, wt.HWND)
        self.assertEqual(library.GetForegroundWindow.argtypes, [])
        self.assertIs(library.GetAncestor.restype, wt.HWND)
        self.assertEqual(library.GetAncestor.argtypes, [wt.HWND, wt.UINT])
        self.assertIs(library.SetClipboardData.restype, wt.HANDLE)
        self.assertEqual(library.SetClipboardData.argtypes, [wt.UINT, wt.HANDLE])
        self.assertEqual(library.GetWindowThreadProcessId.argtypes, [wt.HWND, ctypes.POINTER(wt.DWORD)])
        self.assertIs(library.keybd_event.argtypes[-1], ctypes.c_size_t)
        self.assertIsNone(library.keybd_event.restype)

    def test_kernel32_declares_pointer_and_allocation_sizes(self):
        library = FakeLibrary()
        with patch.object(target.os, "name", "nt"), patch.object(
            target.ctypes, "windll", SimpleNamespace(kernel32=library), create=True
        ):
            self.assertIs(target._kernel_api(), library)
        self.assertIs(library.OpenProcess.restype, wt.HANDLE)
        self.assertEqual(library.GlobalAlloc.argtypes, [wt.UINT, ctypes.c_size_t])
        self.assertIs(library.GlobalAlloc.restype, wt.HGLOBAL)
        self.assertIs(library.GlobalLock.restype, wt.LPVOID)
        self.assertEqual(library.GlobalFree.argtypes, [wt.HGLOBAL])
        self.assertIs(library.GlobalFree.restype, wt.HGLOBAL)

    def test_process_path_keeps_large_handle_and_closes_it(self):
        handle = 0x123456789ABC
        kernel = Mock()
        kernel.OpenProcess.return_value = handle

        def query(actual_handle, flags, buffer, size):
            self.assertEqual(actual_handle, handle)
            self.assertEqual(flags, 0)
            buffer.value = "C:\\Agent\\Agent.exe"
            return 1

        kernel.QueryFullProcessImageNameW.side_effect = query
        with patch.object(target.os, "name", "nt"), patch.object(target, "_kernel_api", return_value=kernel):
            self.assertEqual(target._process_path(123), "C:\\Agent\\Agent.exe")
        kernel.CloseHandle.assert_called_once_with(handle)

    def test_process_path_query_failure_still_closes_handle(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0x123456789ABC
        kernel.QueryFullProcessImageNameW.return_value = 0
        with patch.object(target.os, "name", "nt"), patch.object(target, "_kernel_api", return_value=kernel):
            self.assertEqual(target._process_path(123), "")
        kernel.CloseHandle.assert_called_once_with(0x123456789ABC)

    def test_process_path_open_failure_does_not_close_null_handle(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 0
        with patch.object(target.os, "name", "nt"), patch.object(target, "_kernel_api", return_value=kernel):
            self.assertEqual(target._process_path(123), "")
        kernel.QueryFullProcessImageNameW.assert_not_called()
        kernel.CloseHandle.assert_not_called()

    def test_root_hwnd_keeps_pointer_sized_return(self):
        user32 = Mock()
        user32.GetAncestor.return_value = 0x123456789ABC
        with patch.object(target.os, "name", "nt"), patch.object(target, "_win_api", return_value=user32):
            self.assertEqual(target.WindowsTargetBackend._root_hwnd(0x123456789ABE), 0x123456789ABC)
        user32.GetAncestor.assert_called_once_with(0x123456789ABE, 2)


class ClipboardMemoryTests(unittest.TestCase):
    def setUp(self):
        self.handle = 0x123456789ABC
        self.pointer = 0x223456789ABC
        self.owner = 0x323456789ABC
        self.user32 = Mock()
        self.user32.GetForegroundWindow.return_value = self.owner
        self.user32.IsWindow.return_value = 1
        self.user32.OpenClipboard.return_value = 1
        self.user32.EmptyClipboard.return_value = 1
        self.user32.SetClipboardData.return_value = self.handle
        self.kernel = Mock()
        self.kernel.GlobalAlloc.return_value = self.handle
        self.kernel.GlobalLock.return_value = self.pointer
        patches = (
            patch.object(target, "_win_api", return_value=self.user32),
            patch.object(target, "_kernel_api", return_value=self.kernel),
            patch.object(target.ctypes, "memmove"),
            patch.object(target.WindowsTargetBackend, "_send_key"),
        )
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.memmove, self.send_key = self.mocks[2:]

    def test_success_transfers_handle_once_without_freeing_owned_memory(self):
        text = "中文\U0001f600"
        encoded = (text + "\0").encode("utf-16-le")
        target.WindowsTargetBackend._paste_unicode(text)
        self.kernel.GlobalAlloc.assert_called_once_with(0x0002, len(encoded))
        self.kernel.GlobalLock.assert_called_once_with(self.handle)
        self.memmove.assert_called_once_with(self.pointer, encoded, len(encoded))
        self.kernel.GlobalUnlock.assert_called_once_with(self.handle)
        self.assertEqual(self.user32.IsWindow.call_args_list, [call(self.owner), call(self.owner)])
        self.user32.OpenClipboard.assert_called_once_with(self.owner)
        self.user32.SetClipboardData.assert_called_once_with(13, self.handle)
        self.kernel.GlobalFree.assert_not_called()
        self.user32.CloseClipboard.assert_called_once_with()
        self.send_key.assert_called_once_with(0x56, ctrl=True)

    def test_expected_window_mismatch_refuses_before_touching_clipboard(self):
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            target.WindowsTargetBackend._paste_unicode("文字", expected_window_id=self.owner + 1)
        self.kernel.GlobalAlloc.assert_not_called()
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_expected_window_is_checked_again_before_paste(self):
        self.user32.GetForegroundWindow.side_effect = [self.owner, self.owner + 1]
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            target.WindowsTargetBackend._paste_unicode("文字", expected_window_id=self.owner)
        self.user32.SetClipboardData.assert_called_once_with(13, self.handle)
        self.user32.CloseClipboard.assert_called_once_with()
        self.kernel.GlobalFree.assert_not_called()
        self.send_key.assert_not_called()

    def test_invalidated_owner_refuses_before_paste(self):
        self.user32.IsWindow.side_effect = [1, 0]
        with self.assertRaisesRegex(target.TargetError, "失去焦点"):
            target.WindowsTargetBackend._paste_unicode("文字", expected_window_id=self.owner)
        self.user32.CloseClipboard.assert_called_once_with()
        self.send_key.assert_not_called()

    def test_no_foreground_window_refuses_before_allocating_clipboard_memory(self):
        self.user32.GetForegroundWindow.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "有效焦点"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.user32.IsWindow.assert_not_called()
        self.kernel.GlobalAlloc.assert_not_called()
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_invalid_owner_refuses_before_allocating_clipboard_memory(self):
        self.user32.IsWindow.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "有效焦点"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.user32.IsWindow.assert_called_once_with(self.owner)
        self.kernel.GlobalAlloc.assert_not_called()
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_allocation_failure_does_not_lock_or_paste(self):
        self.kernel.GlobalAlloc.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "准备剪贴板"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.kernel.GlobalLock.assert_not_called()
        self.memmove.assert_not_called()
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_lock_failure_frees_once_without_copying(self):
        self.kernel.GlobalLock.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "剪贴板内存"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.kernel.GlobalFree.assert_called_once_with(self.handle)
        self.memmove.assert_not_called()
        self.kernel.GlobalUnlock.assert_not_called()
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_memory_copy_failure_unlocks_and_frees_once(self):
        self.memmove.side_effect = ValueError("mock copy failure")
        with self.assertRaisesRegex(target.TargetError, "剪贴板内存"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.kernel.GlobalUnlock.assert_called_once_with(self.handle)
        self.kernel.GlobalFree.assert_called_once_with(self.handle)
        self.user32.OpenClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_open_failure_frees_once_without_closing_unopened_clipboard(self):
        self.user32.OpenClipboard.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "打开系统剪贴板"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.kernel.GlobalFree.assert_called_once_with(self.handle)
        self.user32.CloseClipboard.assert_not_called()
        self.send_key.assert_not_called()

    def test_empty_failure_closes_clipboard_and_frees_once(self):
        self.user32.EmptyClipboard.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "清空系统剪贴板"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.user32.SetClipboardData.assert_not_called()
        self.user32.CloseClipboard.assert_called_once_with()
        self.kernel.GlobalFree.assert_called_once_with(self.handle)
        self.send_key.assert_not_called()

    def test_set_data_failure_closes_clipboard_without_double_free(self):
        self.user32.SetClipboardData.return_value = 0
        with self.assertRaisesRegex(target.TargetError, "写入系统剪贴板"):
            target.WindowsTargetBackend._paste_unicode("文字")
        self.user32.CloseClipboard.assert_called_once_with()
        self.kernel.GlobalFree.assert_called_once_with(self.handle)
        self.send_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
