"""重复填入和记录批量删除回归，使用隔离数据，不操作用户正文。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from talkman_input_positions import main_input_role
from talkman_target import TargetSelection, TargetWindow, TargetError
from talkman_target_windows import WindowsTargetBackend
from talkman_records import DeliveryHistory
import test_talkman_revision41 as memory_tests
import test_talkman_record_ui as record_tests


class RepeatedPositionTests(unittest.TestCase):
    setUp = memory_tests.PositionMemoryTests.setUp
    tearDown = memory_tests.PositionMemoryTests.tearDown
    callback = memory_tests.PositionMemoryTests.callback
    begin = memory_tests.PositionMemoryTests.begin
    command = memory_tests.PositionMemoryTests.command
    fill = memory_tests.PositionMemoryTests.fill
    main_support = memory_tests.PositionMemoryTests.main_support

    def test_live_selected_main_is_reused_even_when_another_field_appears(self):
        self.main_support()
        self.begin()
        self.command('confirm')
        self.backend.inputs.append('main-second')
        self.backend.resolve_main_input.side_effect = TargetError('后台扫描暂不可用')
        for _ in range(3):
            self.assertEqual(self.fill()[0], 'ok')
            self.assertFalse(self.controller.needs_position_relocation)
        self.backend.resolve_main_input.assert_not_called()
        self.assertEqual(self.backend.texts['main'], '主框原文追加追加追加')
        self.assertEqual(self.backend.texts['answer'], '回答原文')

    def test_recreated_main_recovers_but_missing_custom_never_falls_back(self):
        self.main_support()
        self.begin()
        self.command('confirm')
        self.backend.inputs = ['main-new', 'answer']
        self.backend.texts['main-new'] = ''
        self.assertEqual(self.fill()[0], 'ok')
        self.backend.resolve_main_input.assert_called_once()
        self.begin()
        self.command('choose', 1)
        self.backend.inputs.remove('answer')
        self.assertEqual(self.fill()[0], 'error')
        self.assertTrue(self.controller.needs_position_relocation)
        self.assertEqual(self.backend.texts['main-new'], '追加')


class WindowsComposerTests(unittest.TestCase):
    def setUp(self):
        self.target = TargetWindow(12, 42, 'ChatGPT.exe', '对话', r'C:\Apps\ChatGPT.exe')
        self.backend = WindowsTargetBackend()
        self.element = SimpleNamespace(CurrentAutomationId='', CurrentName='随录入变化的内容',
                                       CurrentClassName='ProseMirror ProseMirror-focused')
        self.role = ('native', 'chatgpt-prosemirror')

    def test_native_editor_identity_does_not_depend_on_name_or_focused_class(self):
        for label, cls in [('', 'ProseMirror'), ('另一条内容', 'ProseMirror ProseMirror-focused')]:
            self.element.CurrentName = label
            self.element.CurrentClassName = cls
            self.assertEqual(self.backend.input_position_role(self.target, self.element), self.role)
        self.assertIsNone(self.backend.input_position_role(replace(self.target, identity='chrome.exe'), self.element))
        self.assertIsNone(main_input_role(self.target, '', [], native_class='editor'))
        metadata = dict(self.target.metadata(), input_mode='explicit', input_role=list(self.role))
        cleaned = TargetSelection._clean(metadata)
        self.assertEqual(cleaned['input_role'], list(self.role))
        self.assertNotIn('input_role', TargetSelection._clean(dict(metadata, identity='other.exe')))

    def test_minimized_main_is_restored_before_requesting_user_selection(self):
        self.backend.list_input_positions = Mock(side_effect=[[], [], [self.element]])
        self.backend._activate_position_window = Mock()
        self.backend.validate_input_position = Mock()
        with patch('talkman_position_windows.time.sleep'):
            self.assertIs(self.backend.resolve_main_input(self.target, self.role), self.element)
        self.backend._activate_position_window.assert_called_once_with(self.target)

    def test_recovery_never_guesses_between_two_native_editors(self):
        second = SimpleNamespace(**vars(self.element))
        self.backend.list_input_positions = Mock(return_value=[self.element, second])
        self.backend._activate_position_window = Mock()
        self.backend.validate_input_position = Mock()
        with self.assertRaises(TargetError):
            self.backend.resolve_main_input(self.target, self.role)
        self.backend._activate_position_window.assert_not_called()


class BulkRecordTests(unittest.TestCase):
    setUp = record_tests.RecordUITests.setUp
    tearDown = record_tests.RecordUITests.tearDown

    def prepare_records(self, mode):
        a = self.app
        a.set_text('编辑框中的文字不变')
        if mode == 'notebook':
            a.write_history([dict(id=str(i), time='2026-11-09 12:00', text=f'草稿{i}') for i in range(5)])
        else:
            for i in range(5):
                a.deliveries.add(f'投递{i}', '测试 Agent', '测试对话')
        a.open_history(mode)
        return a.history_list.get_children()

    def test_bulk_delete_requires_confirmation_and_keeps_other_records_and_editor(self):
        a = self.app
        for mode in ('notebook', 'delivery'):
            with self.subTest(mode=mode):
                ids = self.prepare_records(mode)
                a.history_list.selection_set(ids[:3])
                a.preview_history()
                self.assertIsNone(a.preview_text)
                self.assertTrue(a.history_restore_button.instate(['disabled']))
                a.delete_history()
                a.update()
                self.assertIn('3 条', a.history_delete_button.cget('text'))
                self.assertEqual(len(a.record_items()), 5)
                self.assertTrue(a.confirm_delete_history())
                self.assertEqual([r['id'] for r in a.record_items()], list(ids[3:]))
                self.assertEqual(a.current_text(), '编辑框中的文字不变')
                loaded = a.load_history() if mode == 'notebook' else DeliveryHistory(a.data_dir).items
                self.assertEqual([r['id'] for r in loaded], list(ids[3:]))

    def test_selection_change_or_tab_switch_cancels_old_confirmation(self):
        a = self.app
        ids = self.prepare_records('notebook')
        a.history_list.selection_set(ids[:2]); a.preview_history(); a.delete_history()
        a.history_list.selection_set(ids[:3])
        self.assertFalse(a.confirm_delete_history())
        a.preview_history()
        self.assertIsNone(a.pending_delete)
        a.delete_history(); a.open_history('delivery')
        self.assertFalse(a.confirm_delete_history())
        self.assertEqual(len(a.saved_history), 5)

    def test_select_all_cancel_and_single_click_toggling(self):
        a = self.app
        ids = self.prepare_records('notebook')
        a.toggle_record_multiselect()
        self.assertEqual(a.history_list.selection(), ())
        a.update()
        x, y, width, height = a.history_list.bbox(ids[0])
        event = SimpleNamespace(y=y+height//2)
        self.assertEqual(a.record_selection_click(event), 'break')
        self.assertEqual(a.history_list.selection(), (ids[0],))
        a.record_selection_click(event)
        self.assertEqual(a.history_list.selection(), ())
        a.select_all_records()
        self.assertEqual(len(a.history_list.selection()), 5)
        a.delete_history(); a.cancel_delete_history()
        self.assertEqual(len(a.saved_history), 5)
        a.delete_history()
        self.assertTrue(a.confirm_delete_history())
        self.assertEqual(a.saved_history, [])
        self.assertEqual(a.current_text(), '编辑框中的文字不变')

    def test_disk_failure_preserves_all_records_and_pending_selection(self):
        a = self.app
        for mode in ('notebook', 'delivery'):
            ids = self.prepare_records(mode)
            a.history_list.selection_set(ids[:3]); a.preview_history(); a.delete_history()
            before = [dict(r) for r in a.record_items()]
            with patch('os.replace', side_effect=OSError('测试写入失败')), patch.object(Path, 'replace', side_effect=OSError('测试写入失败')):
                self.assertFalse(a.confirm_delete_history())
            self.assertEqual(a.record_items(), before)
            self.assertEqual(a.history_list.selection(), ids[:3])
            loaded = a.load_history() if mode == 'notebook' else DeliveryHistory(a.data_dir).items
            self.assertEqual(loaded, before)


if __name__ == '__main__':
    unittest.main()
