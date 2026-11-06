"""免费连接、普通下发、豆包编辑器与系统外观的离线回归。"""
import ctypes
from dataclasses import replace
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from talkman_free_service import FreeService, FreeClient
from talkman_qwen import QwenError, QwenSettings
from talkman_target_windows import WindowsTargetBackend, TargetWindow
from talkman_target import TargetError, FillReceipt
from talkman_system_style import windows_titlebar
import test_talkman_app as app_tests
import test_talkman_delivery_confirm as delivery_tests


class RichEditorTests(unittest.TestCase):
    def setUp(self):
        self.b = WindowsTargetBackend()
        self.e = NS(CurrentControlType=50026, CurrentClassName='tiptap ProseMirror',
                    CurrentName='', CurrentIsEnabled=True, CurrentIsOffscreen=False, CurrentIsPassword=False)
        self.text = NS(DocumentRange=NS(GetAttributeValue=Mock(return_value=False), GetText=Mock(return_value='正文\r\n')))
        def pattern(_element, kind, _name):
            if kind == self.b._TEXT_PATTERN:
                return self.text
            raise TargetError('不支持 ValuePattern')
        self.b._pattern = Mock(side_effect=pattern)

    def test_group_editor_requires_real_editable_text_pattern(self):
        self.assertTrue(self.b._is_editable(self.e))
        for value in (True, None, '不支持', object()):
            self.text.DocumentRange.GetAttributeValue.return_value = value
            self.assertFalse(self.b._is_editable(self.e))

    def test_ordinary_groups_trailing_break_search_and_hidden_are_rejected(self):
        for values in ({'CurrentClassName': '普通面板'}, {'CurrentClassName': 'ProseMirror-trailingBreak'},
                       {'CurrentName': '搜索'}, {'CurrentIsOffscreen': True},
                       {'CurrentIsPassword': True}, {'CurrentIsEnabled': False}):
            with self.subTest(values=values):
                self.assertFalse(self.b._is_editable(NS(**(vars(self.e) | values))))

    def test_find_includes_group_and_rejects_duplicate_editors(self):
        elements = [self.e]
        root = NS(FindAll=lambda scope, control: elements if control == 50026 else [])
        self.b._window_element = Mock(return_value=root)
        self.b._uia = NS(CreatePropertyCondition=lambda prop, val: val)
        self.assertIs(self.b._find_editable(None), self.e)
        elements.append(NS(**vars(self.e)))
        self.assertIsNone(self.b._find_editable(None))

    def prepare_read(self):
        w = TargetWindow(123, 45, 'Doubao.exe', '会话')
        self.b.validate_window = Mock(return_value=w)
        self.b._window_element = Mock(return_value=object())
        self.b._is_descendant_of = Mock(return_value=True)
        self.b._find_editable = Mock(return_value=self.e)
        self.b._uia = NS(CompareElements=lambda a,b: a is b)
        return w

    def test_delivery_read_does_not_require_background_write_support(self):
        w = self.prepare_read()
        handle = self.b.open_delivery(w)
        self.assertEqual(self.b.read_delivery(w, handle), '正文\n')
        with self.assertRaises(TargetError):
            self.b.open_sync(w)

    def test_explicit_readonly_value_never_falls_back_to_text(self):
        w = self.prepare_read()
        self.b._is_editable = Mock(return_value=True)
        self.b._pattern = Mock(return_value=NS(CurrentValue='正文', CurrentIsReadOnly=True))
        with self.assertRaisesRegex(TargetError, '只读'):
            self.b.read_delivery(w, (self.e, w.title))
        self.b._pattern.assert_called_once()


class OrdinaryDeliveryTests(unittest.TestCase):
    setUp = delivery_tests.DeliveryConfirmTests.setUp
    tearDown = delivery_tests.DeliveryConfirmTests.tearDown
    callback = delivery_tests.DeliveryConfirmTests.callback
    fill = delivery_tests.DeliveryConfirmTests.fill

    def test_delivery_does_not_use_sync_and_accepts_updated_title(self):
        b = self.backend
        b.open_delivery = b.open_sync
        b.read_delivery = b.read_sync
        b.send_delivery = b.send_sync
        b.open_sync = Mock(side_effect=TargetError('不能后台写入'))
        original = b.windows[0]
        b.validate_window = lambda _w: replace(original, title='更新后的会话')
        receipt = self.fill()
        self.assertEqual(receipt.title, '更新后的会话')
        self.controller.send_filled(receipt.send_token, self.callback)
        self.assertEqual(delivery_tests.wait_result(self.results)[0], 'ok')
        self.assertEqual(b.sent, 1)
        b.open_sync.assert_not_called()


class FreeServiceTests(unittest.TestCase):
    def test_client_without_vendor_key_uses_only_proxy_and_preserves_edit_protocol(self):
        service = Mock()
        service.call.side_effect = [{'text': '语音正文'}, {'choices':[{'finish_reason':'stop','message':{'content':'{"action":"replace","text":"改写结果"}'}}]}]
        client = FreeClient(service)
        self.assertEqual(client.transcribe(b'audio'), '语音正文')
        self.assertEqual(client.transform('原文', '改写'), '改写结果')
        self.assertEqual([c.args[0] for c in service.call.call_args_list],
                         ['/v1/audio/transcriptions', '/v1/chat/completions'])
        self.assertEqual(set(service.call.call_args.args[1]), {'messages', 'max_tokens'})

    def test_busy_response_cools_down_without_retries_or_paid_fallback(self):
        service = FreeService('.')
        service._connect = Mock(return_value=(12345, 'token'))
        opener = Mock()
        opener.open.side_effect = HTTPError('http://localhost', 503, 'busy', {}, io.BytesIO(b'sensitive'))
        with patch('talkman_free_service.request.build_opener', return_value=opener):
            for _ in range(2):
                with self.assertRaisesRegex(QwenError, '文字保留'):
                    service.call('/v1/chat/completions', {'messages':[]})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(service._connect.call_count, 1)

    def test_ssh_keeps_quoted_path_and_close_does_not_wait_for_connection_lock(self):
        service = FreeService('.')
        service._load_profile = Mock(return_value={'host':'test.example','user':'tester',
            'identity':"C:/Users/Steve's/.ssh/key", 'known_hosts':"C:/Users/Steve's/my hosts", 'token':'x'*40})
        process = Mock()
        process.poll.return_value = None
        with patch('talkman_free_service.shutil.which', return_value='ssh'), \
             patch('talkman_free_service.subprocess.Popen', return_value=process) as launch, \
             patch('talkman_free_service.socket.create_connection'):
            service._connect()
        args = launch.call_args.args[0]
        self.assertIn('UserKnownHostsFile="C:/Users/Steve\'s/my hosts"', args)
        self.assertNotIn('x'*40, ' '.join(args))
        service._lock.acquire()
        try:
            service.close()
        finally:
            service._lock.release()
        with self.assertRaises(QwenError): service._connect()

    def test_only_expected_paths_and_bounded_responses_are_accepted(self):
        service = FreeService('.')
        service._connect = Mock(return_value=(12345, 'token'))
        with self.assertRaises(QwenError): service.call('https://other.example')
        service._connect.assert_not_called()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'x' * (2*1024*1024+1)
        opener = Mock()
        opener.open.return_value = response
        with patch('talkman_free_service.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(QwenError, '过大'):
                service.call('/v1/status')


class AppRevisionTests(unittest.TestCase):
    setUp = app_tests.EditorTests.setUp
    tearDown = app_tests.EditorTests.tearDown
    pump = app_tests.EditorTests.pump

    def test_short_settings_page_does_not_keep_height_of_long_page(self):
        a = self.app
        # 几何测试必须先显示主窗口；Mac 不显示 withdrawn 父窗口的 transient 子窗。
        a.deiconify()
        a.update()
        a.open_settings('about')
        a.update()
        large = a.settings_notebook.winfo_reqheight()
        a.settings_notebook.select(a.settings_tabs['model'])
        a.update()
        self.assertLess(a.settings_notebook.winfo_reqheight(), large)
        self.assertEqual(a.settings_viewport.canvas.yview()[0], 0)

    def test_free_voice_works_without_vendor_key_and_cancel_discards_late_result(self):
        a = self.app
        a.settings = QwenSettings()
        a.free_mode_var.set(True)
        with patch.object(type(a.free_service), 'configured', new_callable=lambda: property(lambda _: True)):
            self.assertTrue(a.start_voice('insert'))
            a.cancel_operation()
            a.events.put(('voice', a.operation_id-1, 'done', '迟到的结果'))
            self.pump()
        self.assertEqual(a.current_text(), '')
        self.assertFalse(a.busy)

    def test_clearing_after_fill_preserves_second_send_and_delays_font_fit(self):
        a = self.app
        a.deiconify()
        a.update()
        a.set_text('第一个段落\n' * 60)
        a.update()
        size = a.text._fit_font.actual('size')
        bounds = (a.text.winfo_height(), a.target_button.winfo_rooty())
        a.clear_after_fill_var.set(True)
        text = a.current_text()
        a._delivery_snapshot = (a.revision, text)
        receipt = FillReceipt('已填入', text, '本地测试', '测试会话', '2026-11-01T12:00:00+08:00', send_token='test')
        a.finish_normal_fill('ok', receipt)
        a.update()
        self.assertEqual(a.current_text(), '')
        self.assertEqual(a.pending_delivery['token'], 'test')
        self.assertEqual(a.text._fit_font.actual('size'), size)
        self.assertEqual((a.text.winfo_height(), a.target_button.winfo_rooty()), bounds)
        a.text.focus_force()  # 程序聚焦不解除冻结。
        a.update()
        self.assertTrue(a.text._delivery_fit_hold)
        a.text.resume_fit_after_interaction()
        self.assertTrue(a.text._delivery_fit_hold)
        deadline = time.monotonic()+.5
        while time.monotonic()<deadline:
            a.update()
            time.sleep(.02)
        self.assertFalse(a.text._delivery_fit_hold)
        self.assertEqual(a.text._fit_font.actual('size'), 16)


def _check_mac_appearance():
    """原生 Cocoa 外观与 Tk 共用进程级应用，单独进程验证其完整生命周期。"""
    import AppKit
    case = AppRevisionTests()
    case.setUp()
    try:
        a = case.app
        a.deiconify()
        a.update()
        a.system_style.native = True
        a.system_style._start()
        a.update()
        item = a.system_style.menu_item
        case.assertIsNotNone(item)
        case.assertTrue(item.button().image().isTemplate())
        for theme, expected in (('dark', 'darkaqua'), ('light', 'aqua')):
            a.system_style.set_theme(theme)
            a.update()
            try:
                actual = str(a.tk.call('::tk::unsupported::MacWindowStyle', 'appearance', a._w))
                case.assertEqual(actual, expected)
            except app_tests.tk.TclError:
                expected_name = AppKit.NSAppearanceNameDarkAqua if theme == 'dark' else AppKit.NSAppearanceNameAqua
                case.assertEqual(AppKit.NSApplication.sharedApplication().appearance().name(), expected_name)
            # 切换系统外观后仍可打开、切页及关闭设置，验证实际使用路径。
            a.open_settings('about')
            a.update()
            large = a.settings_notebook.winfo_reqheight()
            a.settings_notebook.select(a.settings_tabs['model'])
            a.update()
            case.assertLess(a.settings_notebook.winfo_reqheight(), large)
            a._close_settings(a.settings_window)
            a.update()
    finally:
        case.tearDown()
        case.doCleanups()


class NativeAppearanceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'Mac 原生外观')
    def test_mac_menu_template_and_theme_follow_application(self):
        process = subprocess.run([sys.executable, '-u', '-c',
            'import faulthandler; faulthandler.dump_traceback_later(30, exit=True); '
            'from test_talkman_revision28 import _check_mac_appearance; _check_mac_appearance()'],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=40)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)


class SystemAppearanceTests(unittest.TestCase):
    def test_dwm_uses_full_hwnd_and_dark_fallback_and_light_palette(self):
        user, dwm = Mock(), Mock()
        user.GetAncestor.return_value = 0x123456789AB
        values = []
        def setter(hwnd, attr, pointer, size):
            values.append((hwnd, attr, ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value))
            return 1 if attr == 20 else 0
        dwm.DwmSetWindowAttribute.side_effect = setter
        for dark in (True, False):
            values.clear()
            windows_titlebar(99, dark, user=user, dwm=dwm)
            self.assertTrue(all(v[0]==0x123456789AB for v in values))
            self.assertIn((0x123456789AB, 19, int(dark)), values)
            self.assertIn((0x123456789AB, 35, 0 if dark else 0xF5F5F5), values)

    def test_packaged_icons_have_small_and_retina_representations(self):
        folder = Path(__file__).parent / 'TalkMan-assets/app'
        ico = (folder / 'TalkMan.ico').read_bytes()
        count = struct.unpack_from('<H', ico, 4)[0]
        sizes = {ico[6+16*i] or 256 for i in range(count)}
        self.assertTrue({16, 20, 24, 32, 48, 256}.issubset(sizes))
        icns = (folder / 'TalkMan.icns').read_bytes()
        self.assertEqual(icns[:4], b'icns')
        self.assertEqual(struct.unpack_from('>I', icns, 4)[0], len(icns))
        for size in (16, 32, 48, 256, 1024):
            png = (folder / f'TalkMan-{size}.png').read_bytes()
            self.assertEqual(struct.unpack_from('>II', png, 16), (size, size))


if __name__ == '__main__': unittest.main()
