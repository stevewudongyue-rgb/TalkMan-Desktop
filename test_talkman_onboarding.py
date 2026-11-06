"""首次连接、真实控件指引与悬停说明；仅用隔离数据和假语音，不调用服务。"""
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from talkman_onboarding import GUIDE_VERSION, should_offer_guide
from talkman_qwen import QwenError, QwenSettings, load_settings
from test_talkman_app import FakeVoice
import test_talkman_window as window_tests


class GuideTests(unittest.TestCase):
    setUp = window_tests.WindowTests.setUp
    tearDown = window_tests.WindowTests.tearDown

    def guide(self, page=None):
        self.app.show_quick_guide()
        if page is not None:
            self.app.guide.show(page)
        self.app.update()
        return self.app.guide

    def test_first_run_and_old_unconfigured_upgrade_then_skip_is_remembered(self):
        self.assertTrue(should_offer_guide({}, False))
        self.assertFalse(should_offer_guide({'guide_seen': True}, False))
        self.assertFalse(should_offer_guide({'guide_seen': True}, True))
        g = self.guide()
        self.assertIsNone(g.panel)
        self.assertEqual(self.app.grab_current(), g.dialog)
        g.close()
        prefs = self.app.ui_preferences
        self.assertEqual(prefs['guide_version'], GUIDE_VERSION)
        self.assertFalse(should_offer_guide(prefs, False))
        self.assertIsNone(self.app.grab_current())

    def test_missing_and_invalid_key_cannot_advance_or_modify_settings(self):
        g = self.guide(0)
        original = self.app.settings
        self.assertEqual(g.key_entry.cget('show'), '•')
        self.assertEqual(g.origin.get(), 'https://maas.qianwenaiapi.com')
        for key in ('', 'sk-sp-not-supported', 'wrong', 'sk-off line'):
            g.key.set(key)
            g.save_key()
            self.assertEqual(g.page, 0)
            self.assertIs(self.app.settings, original)
            self.assertTrue(g.key_feedback.get())
        self.assertFalse((self.app.data_dir/'qwen-settings.json').exists())

    def test_save_session_key_preserves_models_and_does_not_claim_network_success(self):
        a = self.app
        a.settings = QwenSettings(text_model='qwen-plus', voice_mode='stream')
        g = self.guide(0)
        g.key.set('sk-offline-test-not-a-real-key')
        g.origin.set('https://maas.qianwenaiapi.com')
        g.remember.set(False)
        with patch('talkman_qwen.request.urlopen') as network:
            g.save_key()
        network.assert_not_called()
        self.assertEqual(g.page, 1)
        self.assertEqual(a.settings.voice_mode, 'stream')
        self.assertEqual(a.settings.service_origin, 'https://maas.qianwenaiapi.com')
        self.assertNotIn('成功', a.status_var.get())
        saved = (a.data_dir/'qwen-settings.json').read_text(encoding='utf-8')
        self.assertNotIn('sk-offline', saved)
        self.assertEqual(load_settings(a.data_dir).api_key, '')

    def test_secure_storage_failure_stays_at_key_step_and_preserves_settings(self):
        g = self.guide(0)
        g.key.set('sk-offline-test')
        original = self.app.settings
        with patch('talkman_onboarding.save_settings', side_effect=QwenError('加密存储不可用')):
            g.save_key()
        self.assertEqual(g.page, 0)
        self.assertIs(self.app.settings, original)
        self.assertIn('加密存储', g.key_feedback.get())

    def test_key_cannot_change_while_processing(self):
        g = self.guide(0)
        g.key.set('sk-offline-test')
        self.app.auto_running = True
        try:
            with patch('talkman_onboarding.save_settings') as save:
                g.save_key()
            save.assert_not_called()
            self.assertEqual(g.page, 0)
            self.assertIn('先结束', g.key_feedback.get())
        finally:
            self.app.auto_running = False

    def test_existing_key_is_kept_and_no_rewrite_when_unchanged(self):
        a = self.app
        a.settings = QwenSettings(api_key='sk-offline-existing')
        g = self.guide(0)
        with patch('talkman_onboarding.save_settings') as save:
            g.save_key()
        save.assert_not_called()
        self.assertEqual(a.settings.api_key, 'sk-offline-existing')
        self.assertEqual(g.page, 1)

    def test_more_settings_returns_to_guide_and_empty_close_stays_at_key(self):
        a = self.app
        g = self.guide(0)
        g.more_settings()
        a.update()
        self.assertTrue(g.waiting_settings)
        self.assertIsNone(g.dialog)
        a._close_settings(a.settings_window)
        a.update()
        self.assertEqual(g.page, 0)
        self.assertTrue(g.dialog.winfo_ismapped())
        g.more_settings()
        a.text_settings_fields['key'].set('sk-offline-settings')
        a.text_remember_var.set(False)
        a.text_settings_save()
        a.update()
        self.assertEqual(g.page, 1)
        self.assertFalse(g.modal)

    def test_cards_anchor_to_live_widgets_stay_inside_and_keep_selection(self):
        a = self.app
        a.set_text('选中苹果，保留这份正文。')
        a.text.tag_add('sel', '1.2', '1.4')
        a.text.mark_set('insert', '1.4')
        selection = tuple(map(str, a.text.tag_ranges('sel')))
        g = self.guide()
        for size in ('900x640', '1180x780'):
            a.geometry(size)
            for page, target in ((1, a.replace_button), (2, a.rewrite_button), (3, a.target_selector)):
                g.show(page)
                a.update()
                self.assertIn(target, g.targets)
                if page != 2:
                    self.assertLess(g.panel.winfo_height(), a.winfo_height()//2)
                else:
                    self.assertGreater(g.panel.winfo_width(), a.winfo_width()*.6)
                self.assertGreaterEqual(g.panel.winfo_x(), 0)
                self.assertLessEqual(g.panel.winfo_x()+g.panel.winfo_width(), a.winfo_width())
                self.assertLessEqual(g.panel.winfo_y()+g.panel.winfo_height(), a.winfo_height())
                for w in g.targets:
                    self.assertTrue(w.winfo_ismapped())
                    # 卡片不能盖住本步要操作的真实按钮。
                    self.assertLessEqual(g.panel.winfo_rooty()+g.panel.winfo_height(), w.winfo_rooty())
        self.assertEqual(tuple(map(str, a.text.tag_ranges('sel'))), selection)
        self.assertEqual(a.text.index('insert'), '1.4')
        g.close()
        self.assertEqual(a.current_text(), '选中苹果，保留这份正文。')

    def test_command_introduction_views_and_conversation_preserve_document_without_recording(self):
        a = self.app
        a.set_text('原正文：帮我把苹果拿过来。')
        a.text.tag_add('sel','1.7','1.9')
        a.text.mark_set('insert','1.9')
        original=(a.current_text(),tuple(map(str,a.text.tag_ranges('sel'))),a.text.index('insert'))
        g=self.guide(2)
        with patch.object(a,'start_voice') as record,patch.object(a.app_commands,'handle') as command:
            for view in ('edit','app','dialogue'):
                g.select_command_view(view)
                a.update()
                self.assertEqual(g.panel.winfo_toplevel(),a)
                self.assertIsNone(g.dialog)
                text='\n'.join(w.cget('text') for w in g.command_labels)
                self.assertIn({'edit':'帮我把梨拿过来','app':'存个草稿','dialogue':'没说清楚'}[view],text)
            for _ in range(3):g.advance_conversation()
            self.assertIn('可以，就这样',''.join(w.cget('text') for w in g.command_labels))
            g.advance_conversation()
            self.assertEqual(g.conversation_step,0)
            g.next.invoke()
            self.assertEqual(g.page,3)
            record.assert_not_called()
            command.assert_not_called()
        g.close()
        self.assertEqual((a.current_text(),tuple(map(str,a.text.tag_ranges('sel'))),a.text.index('insert')),original)

    def test_command_intro_scroll_and_footer_stay_accessible_on_small_window(self):
        a=self.app
        a.geometry('900x640')
        g=self.guide(2)
        g.select_command_view('app')
        a.update()
        g.command_canvas.yview_moveto(1)
        a.update()
        first,last=map(float,g.command_canvas.yview())
        self.assertAlmostEqual(last,1,places=2)
        self.assertLessEqual(g.next.winfo_rooty()+g.next.winfo_height(),g.panel.winfo_rooty()+g.panel.winfo_height())
        self.assertLess(g.panel.winfo_rooty()+g.panel.winfo_height(),a.rewrite_button.winfo_rooty())
        a.toggle_theme()
        self.assertEqual(g.command_canvas.cget('bg'),a.palette['input'])
        g.close()
        self.assertIsNone(g.command_canvas)

    def test_settings_can_open_command_introduction_directly(self):
        a=self.app
        a.open_settings('text')
        a.update()
        a.command_guide_button.invoke()
        a.update()
        self.assertEqual(a.guide.page,2)
        self.assertIsNotNone(a.guide.command_canvas)
        self.assertFalse(a.settings_window.winfo_exists())
        self.assertIsNone(a.grab_current())

    def test_tour_allows_local_shortcuts_and_starts_only_on_explicit_action(self):
        a = self.app
        g = self.guide(1)
        self.assertEqual(FakeVoice.sessions, [])
        a.focus_at_pointer = Mock(return_value='break')
        a.shortcuts['focus'] = 'F6'
        a.key_press(SimpleNamespace(keysym='F6', state=0, keycode=0))
        a.focus_at_pointer.assert_called_once()
        self.assertEqual(FakeVoice.sessions, [])
        self.assertFalse(g.modal)

    def test_replay_close_cancels_jobs_and_restores_hotkeys(self):
        a = self.app
        for _ in range(3):
            g = self.guide(1)
            g.resize(SimpleNamespace(widget=a))
            g.close()
            self.assertIsNone(g.job)
            self.assertEqual(g.markers, [])
            self.assertIsNone(g.panel)
            a.update()
            self.assertTrue(self.controller.bindings)
        g = self.guide(0)
        a.close_app()
        self.assertFalse(g.opened)
        self.assertIsNone(g.dialog)

    def test_sync_last_step_explains_actual_send_and_theme_refresh(self):
        a = self.app
        a.sync_mode_var.set(True)
        g = self.guide(3)
        self.assertIn('回车发送', g.body_label.cget('text'))
        a.toggle_theme()
        self.assertTrue(all(w.cget('bg') == a.palette['text'] for w in g.markers))

    def test_help_is_delayed_single_instance_dismissible_and_keeps_focus(self):
        a, help = self.app, self.app.hover_help
        a.open_history('notebook')
        a.text.focus_force()
        a.update()
        a.text.tag_add('sel', '1.0', '1.0')
        focus = a.focus_get()
        help.schedule(a.history_button)
        self.assertIsNotNone(help.job)
        self.assertIsNone(help.window)
        help.show()
        a.update()
        self.assertEqual(a.focus_get(), focus)
        first = help.window
        self.assertIn('还没发出去', help.text)
        help.schedule(a.delivery_button)
        self.assertFalse(first.winfo_exists())
        help.show()
        a.update()
        self.assertIn('时间和目标', help.text)
        self.assertLessEqual(help.window.winfo_rootx()+help.window.winfo_width(), a.winfo_rootx()+a.winfo_width())
        self.assertLessEqual(help.window.winfo_rooty()+help.window.winfo_height(), a.winfo_rooty()+a.winfo_height())
        a.delivery_button.event_generate('<Leave>')
        self.assertIsNone(help.window)
        self.assertIsNone(help.job)

    def test_help_reuses_bindings_preserves_existing_handlers_and_cleans_destroy(self):
        a, help = self.app, self.app.hover_help
        button = a.one_filler_button
        entered = Mock()
        button.bind('<Enter>', lambda _e: entered(), add=True)
        before = button.bind('<Enter>')
        help.attach(button, '换一条说明')
        self.assertEqual(button.bind('<Enter>'), before)
        button.event_generate('<Enter>')
        entered.assert_called_once()
        self.assertIsNotNone(help.job)
        help.show()
        button.event_generate('<ButtonPress-1>')
        self.assertIsNone(help.window)
        w = a.button(a, '测试按钮', tooltip='测试说明')
        w.place(x=100, y=100)
        a.update()
        help.schedule(w)
        w.destroy()
        self.assertIsNone(help.job)

    def test_help_switches_description_with_sync_without_exposing_key(self):
        a, help = self.app, self.app.hover_help
        a.settings.api_key = 'sk-offline-private'
        for enabled, expected in ((False, '再点一次才按回车发送'), (True, '回车发送')):
            a.sync_mode_var.set(enabled)
            help.schedule(a.target_button)
            help.show()
            self.assertIn(expected, help.text)
            self.assertNotIn(a.settings.api_key, help.text)
        a.toggle_theme()
        self.assertIsNone(help.window)

    def test_voice_button_dismisses_help_before_break_without_losing_selection(self):
        a, help = self.app, self.app.hover_help
        a.settings = QwenSettings(api_key='sk-offline-test')
        a.set_text('请把苹果拿过来')
        a.text.tag_add('sel', '1.2', '1.4')
        before = a.current_text()
        for button in (a.replace_button, a.rewrite_button):
            help.schedule(button)
            help.show()
            self.assertIsNotNone(help.window)
            with patch('talkman_app.prepare_microphone_permission', return_value=True):
                button.event_generate('<ButtonPress-1>', x=20, y=20)
            self.assertIsNone(help.window)
            self.assertIsNone(help.job)
            self.assertTrue(a.busy)
            self.assertEqual(a.current_text(), before)
            self.assertTrue(a.get_selection())
            button.event_generate('<ButtonRelease-1>', x=20, y=20)
            a.cancel_operation()
            a.update()

    def test_key_dialog_fits_minimum_window_and_settings_fields_have_help(self):
        a = self.app
        a.geometry('900x640')
        g = self.guide(0)
        self.assertLessEqual(g.dialog.winfo_height(), a.winfo_height())
        self.assertLessEqual(g.dialog.winfo_width(), a.winfo_width())
        self.assertLess(g.key_entry.winfo_rooty(), g.key_next.winfo_rooty())
        g.more_settings()
        a.update()
        help = a.hover_help
        entries = [w for row in a.settings_tabs['model'].winfo_children()
                   for child in row.winfo_children() for w in child.winfo_children()
                   if hasattr(w, 'help_text')]
        self.assertGreaterEqual(len(entries), 6)
        help.schedule(entries[0])
        help.show()
        self.assertIsNotNone(help.window)


if __name__ == '__main__':
    unittest.main()
