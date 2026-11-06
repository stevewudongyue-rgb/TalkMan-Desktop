import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_talkman_app import FakeClient, FakeVoice
from test_talkman_license_support import licensed_app, TestEntitlements
import talkman_updates as updates


class UpdateUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=licensed_app(self.temp.name,session_factory=FakeVoice,client_factory=FakeClient,
                             desktop_services=False,license_client=TestEntitlements(False))
        self.app.withdraw()

    def tearDown(self):
        self.app.busy=False;self.app.auto_running=False;self.app.target_filling=False
        self.app.ai_review=None;self.app.close_app();gc.collect();self.temp.cleanup()

    def ready(self):
        a=self.app;a.update_phase='ready'
        a.update_package=Path(self.temp.name)/'update.zip'
        a.update_stage=Path(self.temp.name)/'stage'
        a.update_installed=Path(self.temp.name)/'app'

    def test_public_edition_has_update_controls_and_busy_guards(self):
        a=self.app;a.open_settings('about');a.update()
        self.assertTrue(a.has_feature('records'))
        self.assertTrue(a.update_check_button.winfo_exists())
        self.assertIn('disabled',a.update_action_button.state())
        self.ready()
        for attribute in ('busy','auto_running','target_filling','_mic_permission_pending'):
            setattr(a,attribute,True)
            with patch('talkman_update_ui.updates.launch_installer') as launch:
                a.install_app_update();launch.assert_not_called()
            setattr(a,attribute,False)
        for obj,attr,value in [(a,'ai_review',{}),(a.live_sync,'inflight',True)]:
            old=getattr(obj,attr);setattr(obj,attr,value)
            with patch('talkman_update_ui.updates.launch_installer') as launch:
                a.install_app_update();launch.assert_not_called()
            setattr(obj,attr,old)

    def test_save_failure_never_signals_install_or_loses_text(self):
        a=self.app;self.ready();a.set_text('尚未保存的正文')
        ready=Path(self.temp.name)/'ready';process=Mock()
        with patch('talkman_update_ui.updates.launch_installer',return_value=(ready,process)), \
             patch.object(a,'persist_current_text',return_value=False):
            a.install_app_update()
        process.terminate.assert_called_once()
        self.assertFalse(ready.exists());self.assertFalse(a.closing)
        self.assertEqual(a.current_text(),'尚未保存的正文')

    def test_success_saves_basic_draft_before_releasing_installer(self):
        a=self.app;self.ready();a.set_text('更新后继续编辑的正文')
        ready=Path(self.temp.name)/'ready';process=Mock()
        with patch('talkman_update_ui.updates.launch_installer',return_value=(ready,process)):
            a.install_app_update()
        self.assertTrue(a.closing);self.assertTrue(ready.exists())
        process.terminate.assert_not_called()
        self.assertEqual((a.data_dir/'current-text.txt').read_text(encoding='utf-8'),'更新后继续编辑的正文')

    def test_closed_settings_can_reopen_with_ready_state(self):
        a=self.app;a.open_settings('about');a._close_settings(a.settings_window)
        self.ready();a.update_message.set('已准备好')
        a.open_settings('about');a.update()
        self.assertNotIn('disabled',a.update_action_button.state())
        self.assertEqual(a.update_message.get(),'已准备好')

    def test_check_result_cannot_trigger_install(self):
        a=self.app
        with patch.object(a,'run_worker') as worker, patch('talkman_update_ui.updates.launch_installer') as launch:
            a.check_app_update()
            callback=worker.call_args.args[1]
            callback('ok',updates.UpdateInfo('0.1.34','windows-x64','url','a'*64,42))
            self.assertEqual(a.update_phase,'available');launch.assert_not_called()
        a.open_settings('about');a.update()
        self.assertNotIn('disabled',a.update_action_button.state())

    def test_upgrade_resumes_selection_and_preferences_without_reonboarding(self):
        a=self.app
        a.deiconify();a.geometry('960x720+30+30');a.update()
        a.set_text('升级后继续编辑，原有文字保留。')
        a.text.tag_add('sel','1.5','1.9')
        a.text.mark_set('insert','1.9')
        expected=a.get_selection()[2]
        a.set_theme('swiss')
        a.save_ui_preferences(guide_seen=True, guide_version=1, auto_delay=13, only_agents=False)
        a.save_update_workspace()
        self.assertNotIn(a.current_text(),(a.data_dir/'update-workspace.json').read_text())
        a.close_app()
        self.app=licensed_app(self.temp.name,session_factory=FakeVoice,client_factory=FakeClient,
                             desktop_services=False,license_client=TestEntitlements(False))
        a=self.app;a.update()
        self.assertEqual(a.current_text(),'升级后继续编辑，原有文字保留。')
        self.assertEqual(a.text.index('insert'),'1.9')
        self.assertEqual(a.get_selection()[2],expected)
        self.assertEqual(a.theme_key,'swiss')
        self.assertEqual(a.auto_delay_var.get(),13)
        self.assertFalse(a.only_agents_var.get())
        self.assertFalse((a.data_dir/'update-workspace.json').exists())
        from talkman_onboarding import should_offer_guide
        self.assertFalse(should_offer_guide(a.ui_preferences,False))

    def test_changed_document_does_not_restore_old_selection(self):
        a=self.app;a.set_text('旧内容');a.save_update_workspace()
        a.set_text('新的正文')
        a.restore_update_workspace()
        self.assertEqual(a.current_text(),'新的正文')
        self.assertFalse(a.get_selection())


if __name__=='__main__':unittest.main()
