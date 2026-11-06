"""本轮窗口、偏好与新手引导回归；不访问用户数据或真实麦克风。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import test_talkman_window as window_tests
from talkman_target import TargetWindow, is_agent_window
from talkman_qwen import QwenSettings
from test_talkman_app import FakeVoice


class SessionModeTests(unittest.TestCase):
    setUp = window_tests.WindowTests.setUp
    tearDown = window_tests.WindowTests.tearDown
    dock = window_tests.WindowTests.dock
    tick = window_tests.WindowTests.tick

    def begin(self):
        self.app.settings = QwenSettings(api_key='offline')
        self.dock()
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            self.controller.trigger('global_record')
            self.app.update()
        return FakeVoice.sessions[-1]

    def done(self, text):
        self.app.finish_voice(text)
        self.app.cancel_auto_timer()

    def test_long_speech_expands_same_editor_and_returns_to_tab(self):
        a, c = self.app, self.controller
        a.set_text('前文')
        editor = a.text
        voice = self.begin()
        a.show_voice_text('新'*120)
        self.assertTrue(c.compact)
        a.show_voice_text('新'*121)
        self.assertFalse(c.compact)
        self.assertIs(a.text, editor)
        self.assertIs(a.voice, voice)
        self.assertFalse(voice.stopped)
        a.show_voice_text('新'*150)
        self.done('新'*150)
        self.tick()
        self.tick(2.1)
        self.assertTrue(c.hidden)
        c.reveal()
        a.undo()
        self.assertEqual(a.current_text(), '前文')

    def test_long_existing_document_does_not_trigger_expansion(self):
        self.app.set_text('旧'*600)
        self.begin()
        self.app.show_voice_text('短句')
        self.assertTrue(self.controller.compact)
        self.done('短句')

    def test_file_result_expands_and_mac_finishes_back_to_tab(self):
        self.controller.automatic_hide = False
        self.begin()
        self.done('最终'*100)
        self.assertFalse(self.controller.compact)
        self.tick()
        self.tick(2.1)
        self.assertTrue(self.controller.hidden)

    def test_pending_cleanup_blocks_auto_return(self):
        self.begin()
        self.done('长'*121)
        a = self.app
        a.auto_job = a.after(60000, lambda: None)
        self.tick(10)
        self.assertFalse(self.controller.hidden)
        a.cancel_auto_timer()
        a.auto_running = True
        self.tick(10)
        self.assertFalse(self.controller.hidden)
        a.auto_running = False
        self.tick()
        self.tick(2.1)
        self.assertTrue(self.controller.hidden)

    def test_manual_takeover_and_error_keep_full_window_open(self):
        self.begin()
        self.app.show_voice_text('长'*121)
        self.controller.touch(SimpleNamespace(widget=self.app.text))
        self.done('长'*121)
        self.tick(20)
        self.assertFalse(self.controller.hidden)
        self.assertTrue(self.controller.expanded_hold_open)

    def test_failed_voice_keeps_error_visible_and_original_text(self):
        self.app.set_text('原文')
        self.begin()
        self.app.show_voice_text('长'*121)
        self.app.restore_voice('测试错误')
        self.tick(20)
        self.assertFalse(self.controller.hidden)
        self.assertEqual(self.app.current_text(), '原文')

    def test_wake_only_does_not_record_and_works_with_voice_disabled(self):
        self.dock()
        a, c = self.app, self.controller
        a.global_voice_var.set(False)
        c.sync_hotkeys()
        self.assertEqual(set(c.bindings), {'global_show'})
        a.start_voice = Mock()
        c.hotkey_press('global_show')
        a.update()
        self.assertFalse(c.hidden)
        self.assertFalse(c.compact)
        a.start_voice.assert_not_called()
        self.assertEqual(a.focus_get(), a.text)

    def test_hold_release_stops_same_recording(self):
        a, c = self.app, self.controller
        a.settings = QwenSettings(api_key='offline')
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            c.hotkey_press('global_record')
            a.update()
        voice = a.voice
        c.hotkey_release('global_record', 0.7)
        self.assertTrue(voice.stopped)

    def test_short_click_then_click_stops_and_toggle_ignores_long_release(self):
        a, c = self.app, self.controller
        a.settings = QwenSettings(api_key='offline')
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            c.hotkey_press('global_record')
            a.update()
        voice = a.voice
        c.hotkey_release('global_record', 0.05)
        self.assertFalse(voice.stopped)
        c.hotkey_press('global_record')
        self.assertTrue(voice.stopped)
        c.hotkey_release('global_record', 0.4)

    def test_hold_released_before_idle_never_opens_microphone(self):
        a, c = self.app, self.controller
        a.voice_gesture_var.set('hold')
        a.start_voice = Mock()
        c.hotkey_press('global_record')
        c.hotkey_release('global_record', 0.01)
        a.update()
        a.start_voice.assert_not_called()

    def test_stale_release_cannot_stop_new_operation(self):
        a, c = self.app, self.controller
        a.settings = QwenSettings(api_key='offline')
        with patch('talkman_app.prepare_microphone_permission', return_value=True):
            c.hotkey_press('global_record')
            a.update()
        a.operation_id += 1
        a.stop_voice = Mock()
        c.hotkey_release('global_record', 1)
        a.stop_voice.assert_not_called()

    def test_cleanup_preferences_persist_false_without_losing_other_values(self):
        a = self.app
        a.save_ui_preferences(custom='保留')
        a.filler_var.set(False)
        a.punct_var.set(True)
        a.layout_var.set(False)
        a.auto_changed()
        prefs = json.loads(a.ui_path.read_text(encoding='utf-8'))
        self.assertEqual((prefs['auto_filler'], prefs['auto_punct'], prefs['auto_layout']), (False, True, False))
        self.assertEqual(prefs['custom'], '保留')

    def test_agent_filter_uses_identity_and_browser_title(self):
        a = self.app
        agent = TargetWindow(1, 1, '豆包', '新对话', 'doubao')
        browser = TargetWindow(2, 2, 'Chrome', 'ChatGPT - Chrome', 'chrome.exe')
        normal = TargetWindow(3, 3, 'Chrome', '普通网页', 'chrome.exe')
        self.assertTrue(is_agent_window(agent))
        self.assertTrue(is_agent_window(browser))
        self.assertFalse(is_agent_window(normal))
        a.target_windows = {x.id: x for x in (agent, browser, normal)}
        self.assertEqual(len(a.target_window_rows()), 2)
        a.only_agents_var.set(False)
        self.assertEqual(len(a.target_window_rows()), 3)

    def test_guide_is_embedded_skippable_replayable_and_does_not_edit(self):
        a = self.app
        a.set_text('不能改动的草稿')
        a.show_quick_guide()
        a.update()
        self.assertEqual(self.controller.bindings, {})
        guide = a.guide
        self.assertTrue(guide.dialog.winfo_ismapped())
        guide.show(0)
        a.update()
        self.assertTrue(guide.key_entry.winfo_ismapped())
        for page in range(1, 4):
            guide.show(page)
            a.update()
            self.assertLessEqual(guide.next.winfo_rooty()+guide.next.winfo_height(), a.winfo_rooty()+a.winfo_height())
        guide.close()
        self.assertEqual(a.current_text(), '不能改动的草稿')
        self.assertTrue(a.ui_preferences['guide_seen'])
        a.show_quick_guide()
        self.assertTrue(a.guide.opened)

    def test_sync_ui_coalesces_latest_text_and_only_button_sends(self):
        a = self.app
        callbacks = []
        with patch.object(a.target_controller, 'sync', side_effect=lambda text, callback, send=False: callbacks.append((text, callback, send))):
            a.set_sync_mode(True)
            a.set_text('第一句')
            a.live_sync.cancel_timer()
            a.live_sync.flush()
            self.assertEqual(callbacks[0][::2], ('第一句', False))
            a.set_text('最终句')
            callbacks[0][1]('ok', '第一句')
            a.poll_events()
            a.after_cancel(a.poll_job)
            a.poll_job = None
            a.live_sync.cancel_timer()
            a.live_sync.flush()
            self.assertEqual(callbacks[-1][0], '最终句')
            callbacks[-1][1]('ok', '最终句')
            a.poll_events()
            a.after_cancel(a.poll_job)
            a.poll_job = None
            a.fill_to_target()
            self.assertEqual(callbacks[-1][::2], ('最终句', True))
            self.assertEqual(a.target_button.cget('text'), '下发')
            self.assertTrue(a.target_filling)
            callbacks[-1][1]('error', '测试：未发送')
            a.poll_events()
            a.after_cancel(a.poll_job)
            a.poll_job = None
            self.assertTrue(a.live_sync.paused)
            self.assertFalse(a.target_filling)

    def test_sync_off_discards_late_reply_without_new_writes(self):
        a = self.app
        a.set_sync_mode(True)
        a.live_sync.inflight = True
        epoch = a.live_sync.epoch
        a.set_sync_mode(False)
        a.live_sync.finish((epoch, '旧文字', False), 'ok', '旧文字')
        self.assertFalse(a.live_sync.paused)
        self.assertIsNone(a.live_sync.job)
        self.assertEqual(a.target_button.cget('text'), '填入 Agent')
