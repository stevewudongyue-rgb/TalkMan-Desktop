"""优先词的本轮录音核对：仅使用假麦克风与假文字模型。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from talkman_qwen import QwenSettings
from test_talkman_app import FakeVoice, FakeClient
from test_talkman_license_support import licensed_app


class PriorityDictationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='TalkMan-priority-')
        self.app = licensed_app(Path(self.folder.name), session_factory=FakeVoice,
                                file_session_factory=FakeVoice, client_factory=FakeClient,
                                desktop_services=False)
        self.app.withdraw()
        self.app.settings = QwenSettings(api_key='sk-offline-test')
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.app.run_worker = Mock()
        self.permission = patch('talkman_app.prepare_microphone_permission', return_value=True)
        self.permission.start()

    def tearDown(self):
        self.app.close_app()
        self.permission.stop()
        FakeVoice.sessions.clear()
        self.folder.cleanup()

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job = None

    def finish_voice(self, text):
        FakeVoice.sessions[-1].emit('done', text)
        self.pump()

    def reply(self, result, state='ok'):
        kind, token, _ = self.app.run_worker.call_args.args
        self.app.events.put((kind, token, state, result))
        self.pump()

    def test_new_misrecognition_corrected_once_and_undo_is_atomic(self):
        a = self.app
        a.conventions.put('Colo Play', priority=True)
        a.set_text('前文。')
        a.start_voice('insert')
        self.finish_voice('讨论科洛普雷。')
        self.assertEqual(a.run_worker.call_args.args[0], 'dictation_conventions')
        self.assertEqual(a.current_text(), '前文。')
        self.reply('讨论Colo Play。')
        self.assertEqual(a.current_text(), '前文。讨论Colo Play。')
        a.undo()
        self.assertEqual(a.current_text(), '前文。')

    def test_invalid_change_and_service_failure_keep_original(self):
        a = self.app
        a.conventions.put('Colo Play', priority=True)
        for state, result in (('ok', '讨论Colo Play！'), ('error', '服务繁忙')):
            a.set_text('')
            a.start_voice('insert')
            self.finish_voice('讨论科洛普雷。')
            self.reply(result, state)
            self.assertEqual(a.current_text(), '讨论科洛普雷。')
            self.assertFalse(a.busy)

    def test_cancel_and_stale_result_cannot_replace_new_edit(self):
        a = self.app
        a.conventions.put('Colo Play', priority=True)
        a.start_voice('insert')
        self.finish_voice('科洛普雷')
        a.cancel_operation()
        a.set_text('新的正文')
        self.reply('Colo Play')
        self.assertEqual(a.current_text(), '新的正文')
        a.start_voice('insert')
        self.finish_voice('科洛普雷')
        a.revision += 1
        a.render_text('等待模型时修改的正文')
        self.reply('Colo Play')
        self.assertEqual(a.current_text(), '等待模型时修改的正文')
        self.assertFalse(a.busy)

    def test_no_text_model_or_code_mode_skips_extra_request(self):
        a = self.app
        a.conventions.put('Colo Play', priority=True)
        a.start_voice('insert')
        with patch.object(a, 'text_model_ready', return_value=False):
            self.finish_voice('科洛普雷')
        self.assertEqual(a.current_text(), '科洛普雷')
        a.run_worker.assert_not_called()
        a.conventions.set_language('python')
        a.start_voice('insert')
        self.finish_voice('科洛普雷')
        a.run_worker.assert_not_called()

    def test_role_conversion_still_runs_after_priority_check(self):
        a = self.app
        a.conventions.put('Colo Play', priority=True)
        a.speaker_name_var.set('Steve')
        a.speaker_enabled_var.set(True)
        a.start_voice('insert')
        self.finish_voice('我喜欢科洛普雷。')
        self.reply('我喜欢Colo Play。')
        self.assertEqual(a.run_worker.call_args.args[0], 'speech_expression')
        self.reply('Steve 喜欢Colo Play。')
        self.assertEqual(a.current_text(), 'Steve 喜欢Colo Play。')
        a.undo()
        self.assertEqual(a.current_text(), '')


if __name__ == '__main__':
    unittest.main()
