"""全新／旧配置的开关默认值、重启记忆和引导完成记忆。"""
from test_talkman_license_support import licensed_app
import gc
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from talkman_app import TalkManApp
from test_talkman_app import FakeVoice, FakeClient


class PreferenceTests(unittest.TestCase):
    def test_first_run_defaults_and_saved_false_survive_restart(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            for round in range(2):
                app = licensed_app(folder, session_factory=FakeVoice, client_factory=FakeClient, desktop_services=False)
                try:
                    self.assertEqual(tuple(v.get() for v in (app.filler_var, app.punct_var, app.layout_var)),
                                     (True, True, True) if round == 0 else (False, True, False))
                    self.assertEqual(app.preview_ai_var.get(), round == 1)
                    self.assertEqual(app.rewrite_var.get(), round == 0)
                    if round == 0:
                        app.filler_var.set(False)
                        app.layout_var.set(False)
                        app.rewrite_var.set(False)
                        app.preview_ai_var.set(True)
                        app.preview_preference_changed()
                        app.auto_changed()
                        app.save_ui_preferences(guide_seen=True, only_agents=False, voice_gesture='hold')
                    else:
                        self.assertTrue(app.ui_preferences['guide_seen'])
                        self.assertFalse(app.only_agents_var.get())
                        self.assertEqual(app.voice_gesture_var.get(), 'hold')
                finally:
                    app.close_app()
                    gc.collect()

    def test_save_failure_is_visible_and_does_not_claim_persistence(self):
        with TemporaryDirectory() as directory:
            app = licensed_app(Path(directory), session_factory=FakeVoice, client_factory=FakeClient, desktop_services=False)
            try:
                app.filler_var.set(False)
                with patch.object(app, 'save_ui_preferences', side_effect=OSError('离线测试')):
                    app.auto_changed()
                self.assertFalse(app.filler_var.get())
                self.assertIn('记住', app.status_var.get())
                self.assertNotIn('auto_filler', app.ui_preferences)
            finally:
                app.close_app()
