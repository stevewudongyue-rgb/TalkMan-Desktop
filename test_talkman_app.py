"""不录音、不联网：验证语音操作对实际 Tk 编辑器的事务边界。"""
from test_talkman_license_support import licensed_app
import tempfile
import gc
import tkinter as tk
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from talkman_app import TalkManApp, TextHistory
from talkman_qwen import QwenError, QwenSettings, TextEdit
from talkman_asr import ASRSettings, PROVIDERS
from talkman_voice import VoicePermissionError


class FakeVoice:
    sessions=[]
    def __init__(self,key,model,callback,**kw):
        self.callback=callback
        self.kwargs=kw
        self.stopped=self.cancelled=False
        self.sessions.append(self)
    def start(self): pass
    def stop(self): self.stopped=True
    def cancel(self): self.cancelled=True
    def emit(self,kind,value): self.callback(kind,value)


class FakeClient:
    def __init__(self,settings): pass
    def transcribe(self,audio): return "无真实音频"
    def transform(self,text,instruction): return text.replace("苹果","梨")
    def edit_selection(self, document, start, end, instruction):
        return TextEdit(start, end, self.transform(document[start:end], instruction))
    def edit_by_instruction(self,before,after,instruction):
        return TextEdit(len(before),len(before),instruction.removeprefix("在这里插入"))


class TimelineTests(unittest.TestCase):
    def test_branch_after_undo_and_duplicate_commit(self):
        h=TextHistory()
        for text in ("一","二","二","三"): h.commit(text)
        self.assertEqual(h.undo(),"二")
        h.commit("新分支")
        self.assertEqual(h.redo(),"新分支")
        self.assertEqual(h.undo(),"二")


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe=tk.Tk(); probe.withdraw(); probe.destroy()
        except tk.TclError as ex:
            raise unittest.SkipTest("本机 Tcl/Tk 不能初始化："+str(ex))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="TalkMan-test-")
        self.permission_patch=patch("talkman_app.prepare_microphone_permission",return_value=True)
        self.permission=self.permission_patch.start()
        self.addCleanup(self.permission_patch.stop)
        self.app=licensed_app(Path(self.temp.name),session_factory=FakeVoice,client_factory=FakeClient,file_session_factory=FakeVoice)
        self.app.withdraw()
        self.app.settings=QwenSettings(api_key="sk-test-no-network")
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job=None

    def tearDown(self):
        self.app.close_app()
        self.app=None
        FakeVoice.sessions.clear()
        gc.collect()
        self.temp.cleanup()

    def select(self,start,end):
        self.app.text.tag_remove("sel","1.0","end")
        self.app.text.tag_add("sel",start,end)

    def pump(self):
        self.app.poll_events()
        self.app.after_cancel(self.app.poll_job)
        self.app.poll_job=None

    def emit(self,kind,value):
        FakeVoice.sessions[-1].emit(kind,value)
        self.pump()

    def test_stream_replace_is_one_undo_step(self):
        a=self.app
        a.set_text("帮我把苹果拿过来")
        self.select("1.3","1.5")
        self.assertTrue(a.start_voice("replace"))
        self.emit("partial","香")
        self.emit("partial","香蕉")
        self.assertEqual(a.current_text(),"帮我把香蕉拿过来")
        self.emit("done","香蕉")
        a.undo()
        self.assertEqual(a.current_text(),"帮我把苹果拿过来")
        a.redo()
        self.assertEqual(a.current_text(),"帮我把香蕉拿过来")

    def test_long_live_input_can_scroll_without_jumping_back_to_cursor(self):
        a = self.app
        a.deiconify()
        a.geometry('860x620+40+40')
        a.update()
        original = ('这是一次较长的语音记录，需要在录入时回看上文。' * 8 + '\n') * 65
        a.set_text(original)
        a.text.mark_set('insert', 'end-1c')
        with patch.object(a, 'focus_displayof', return_value=a.text):
            self.assertTrue(a.start_voice('insert'))
        self.emit('partial', '第一句')
        a.text.yview_moveto(0)
        a.editor_wheel(SimpleNamespace(state=0, num=None, delta=-120))
        a.update()
        top = a.text.index('@0,0')
        self.assertTrue(a._voice_review_scroll)
        self.emit('partial', '第一句，第二句')
        a.update()
        self.assertEqual(a.text.index('@0,0'), top)
        self.assertEqual(a.current_text(), original + '第一句，第二句')
        self.emit('done', '第一句，第二句')
        self.assertEqual(a.current_text(), original + '第一句，第二句')
        self.assertEqual(a.text.index('@0,0'), top)

    def test_mic_inserts_at_selection_end(self):
        a=self.app
        a.set_text("苹果香蕉")
        self.select("1.0","1.2")
        a.start_voice("insert")
        self.emit("done","和")
        self.assertEqual(a.current_text(),"苹果和香蕉")

    def test_recorded_range_is_emphasized_during_input_then_leaves_caret_at_end(self):
        a=self.app
        a.set_text("请拿苹果过来")
        self.select("1.2","1.4")
        a.start_voice("replace")
        self.assertEqual(a.get_selection()[2],"苹果")
        self.assertEqual(a.text.get(*a.text.tag_ranges("operation")),"苹果")
        self.emit("partial","香蕉")
        self.assertEqual(a.get_selection()[2],"香蕉")
        self.emit("done","香蕉和梨")
        self.assertIsNone(a.get_selection())
        self.assertEqual(a.text.get("1.0", "insert"), "请拿香蕉和梨")
        self.select("1.2", "1.6")
        a.start_voice("replace")
        self.emit("done","葡萄")
        self.assertEqual(a.current_text(),"请拿葡萄过来")
        self.assertIsNone(a.get_selection())
        self.assertEqual(a.text.get("1.0", "insert"), "请拿葡萄")
        a.cancel_operation()
        self.assertIsNone(a.get_selection())
        self.assertEqual(a.current_text(),"请拿葡萄过来")

    def test_cancel_restores_original_selection(self):
        a=self.app
        a.set_text("😀苹果香蕉")
        self.select("1.0 + 1 chars","1.0 + 3 chars")
        a.start_voice("replace")
        self.emit("partial","一段长的新内容")
        a.cancel_operation()
        self.assertEqual(a.current_text(),"😀苹果香蕉")
        self.assertEqual(a.get_selection()[2],"苹果")

    def test_caret_context_edit_before_after_and_delete_are_local_and_undoable(self):
        a=self.app
        original="😀苹果。\n香蕉。"
        for edit, expected, selected in (
            (TextEdit(1,3,"梨"),"😀梨。\n香蕉。","梨"),
            (TextEdit(5,7,"葡萄"),"😀苹果。\n葡萄。","葡萄"),
            (TextEdit(1,4,""),"😀\n香蕉。",None),
        ):
            with self.subTest(edit=edit):
                a.set_text(original)
                a.text.mark_set("insert","2.0")
                with patch.object(a,"focus_displayof",return_value=a.text):
                    self.assertTrue(a.start_voice("rewrite"))
                with patch.object(a,"run_worker") as worker, patch.object(
                        FakeClient,"edit_by_instruction",return_value=edit):
                    self.emit("done","修改光标前后的文字")
                    kind,token,work=worker.call_args.args
                    a.events.put((kind,token,"ok",work()))
                self.pump()
                self.assertEqual(a.current_text(),expected)
                self.assertEqual(a.get_selection()[2] if a.get_selection() else None,selected)
                a.undo()
                self.assertEqual(a.current_text(),original)
                a.redo()
                self.assertEqual(a.current_text(),expected)

    def test_out_of_bounds_context_edit_preserves_original(self):
        a=self.app
        a.set_text("原文")
        with patch.object(a,"focus_displayof",return_value=a.text):
            a.start_voice("rewrite")
        a.commit_voice(TextEdit(0,100,"错误"))
        self.assertFalse(a.busy)
        self.assertEqual(a.current_text(),"原文")

    def test_other_provider_does_not_require_qwen_for_insert_or_replace(self):
        a=self.app
        a.settings.api_key=""
        a.asr_settings=ASRSettings("baidu",{"baidu":{"api_key":"offline-baidu"}})
        a.set_text("苹果香蕉")
        self.select("1.0","1.2")
        with patch("talkman_app.transcriber",return_value=lambda _:"梨"):
            self.assertTrue(a.start_voice("replace"))
        self.assertEqual(FakeVoice.sessions[-1].kwargs["max_seconds"],60)
        self.emit("done","梨")
        self.assertEqual(a.current_text(),"梨香蕉")
        a.undo()
        self.select("1.0","1.2")
        with patch("talkman_app.transcriber",return_value=lambda _:"关闭自动标点"):
            self.assertTrue(a.start_voice("rewrite"))
        self.emit('done', '关闭自动标点')
        self.assertFalse(a.punct_var.get())
        self.assertEqual(a.current_text(),"苹果香蕉")
        with patch("talkman_app.transcriber",return_value=lambda _:"把苹果改成梨"):
            self.assertTrue(a.start_voice("rewrite"))
        self.emit('done', '把苹果改成梨')
        self.assertIn('文字模型', a.status_var.get())
        self.assertEqual(a.current_text(),"苹果香蕉")

    def test_asr_dialog_all_providers_and_theme_keep_document(self):
        a=self.app
        a.set_text("这是不会因为切换主题丢失的正文。")
        a.toggle_theme()
        a.open_asr_settings()
        for name,spec in PROVIDERS.items():
            a.asr_provider_choice.set(spec.label)
            a.asr_provider_combobox.event_generate("<<ComboboxSelected>>")
            a.asr_window.update_idletasks()
            self.assertEqual(set(a.asr_fields),{item.name for item in spec.fields})
        self.assertEqual(a.current_text(),"这是不会因为切换主题丢失的正文。")
        a.asr_window.destroy()

    def test_error_cancel_and_late_result_preserve_original(self):
        a=self.app
        a.set_text("首行\n苹果  ")
        self.select("2.0","2.2")
        a.start_voice("replace")
        old=FakeVoice.sessions[-1]
        self.emit("partial","错误文字")
        a.cancel_operation()
        old.emit("done","不应覆盖")
        self.pump()
        self.assertEqual(a.current_text(),"首行\n苹果  ")
        self.select("2.0","2.2")
        a.start_voice("replace")
        self.emit("partial","识别中")
        self.emit("error","断网")
        self.assertEqual(a.current_text(),"首行\n苹果  ")

    def test_instruction_uses_original_selection_not_command(self):
        a=self.app
        a.set_text("请：帮我把苹果拿过来。谢谢")
        self.select("1.2","1.11")
        a.start_voice("rewrite")
        self.emit("partial","把苹果改成梨")
        self.assertEqual(a.current_text(),"请：帮我把苹果拿过来。谢谢")
        with patch.object(a,"run_worker") as worker:
            self.emit("done","把苹果改成梨")
            kind,token,work=worker.call_args.args
            a.events.put((kind,token,"ok",work()))
        self.pump()
        self.assertEqual(a.current_text(),"请：帮我把梨拿过来。谢谢")

    def test_delete_entire_selected_text_applies_empty_model_result_and_is_undoable(self):
        a = self.app
        original = "前文\n这一段全部删除。\n后文"
        a.set_text(original)
        self.select("2.0", "2.9")
        self.assertTrue(a.start_voice("rewrite"))
        with patch.object(a, "run_worker") as worker:
            self.emit("done", "全部去掉吧，这一段。")
            kind, token, _ = worker.call_args.args
            a.events.put((kind, token, "ok", ""))
        self.pump()
        self.assertEqual(a.current_text(), "前文\n\n后文")
        self.assertIsNone(a.get_selection())
        self.assertEqual(a.text.index("insert"), "2.0")
        a.undo()
        self.assertEqual(a.current_text(), original)

    def test_instruction_without_selection_inserts_at_captured_caret_as_one_undo(self):
        a=self.app
        original="开头😀\n结尾  "
        a.set_text(original)
        a.text.mark_set("insert","2.0")
        with patch.object(a,"focus_displayof",return_value=a.text):
            self.assertTrue(a.start_voice("rewrite"))
        self.emit("partial","在这里插入新的内容")
        self.assertEqual(a.current_text(),original)
        with patch.object(a,"run_worker") as worker, patch.object(
                FakeClient,"edit_by_instruction",return_value=TextEdit(4,4,"新的内容\n")) as insert:
            self.emit("done","在这里插入新的内容")
            kind,token,work=worker.call_args.args
            # 录音启动后改变 insert 标记，不得把模型结果写到新位置。
            a.text.mark_set("insert","end-1c")
            a.events.put((kind,token,"ok",work()))
            insert.assert_called_once_with("开头😀\n","结尾  ","在这里插入新的内容")
        self.pump()
        self.assertEqual(a.current_text(),"开头😀\n新的内容\n结尾  ")
        self.assertEqual(a.text.get("1.0","insert"),"开头😀\n新的内容\n")
        a.undo()
        self.assertEqual(a.current_text(),original)
        a.redo()
        self.assertEqual(a.current_text(),"开头😀\n新的内容\n结尾  ")

    def test_instruction_without_selection_or_editor_focus_can_control_app(self):
        a=self.app
        a.set_text("原文保持不动")
        count=len(FakeVoice.sessions)
        with patch.object(a,"focus_displayof",return_value=None):
            self.assertTrue(a.start_voice("rewrite"))
        self.assertEqual(len(FakeVoice.sessions),count+1)
        self.permission.assert_called_once()
        self.emit('done', '关闭自动标点')
        self.assertEqual(a.current_text(),"原文保持不动")
        self.assertFalse(a.punct_var.get())

    def test_instruction_requiring_selection_leaves_text_and_history_unchanged(self):
        a=self.app
        a.set_text("请帮我把苹果拿过来。")
        a.text.mark_set("insert","1.3")
        history=(a.timeline.items.copy(),a.timeline.index)
        with patch.object(a,"focus_displayof",return_value=a.text):
            self.assertTrue(a.start_voice("rewrite"))
        with patch.object(a,"run_worker") as worker, patch.object(
                FakeClient,"edit_by_instruction",side_effect=QwenError("请先框选需要替换的文字")):
            self.emit("done","把苹果改成梨")
            kind,token,work=worker.call_args.args
            try:
                work()
            except QwenError as ex:
                a.events.put((kind,token,"error",str(ex)))
        self.pump()
        self.assertFalse(a.busy)
        self.assertEqual(a.current_text(),"请帮我把苹果拿过来。")
        self.assertEqual(a.text.get("1.0","insert"),"请帮我")
        self.assertEqual((a.timeline.items,a.timeline.index),history)
        self.assertIn("请先框选",a.status_var.get())

    def test_cancelled_caret_instruction_ignores_late_model_result(self):
        a=self.app
        a.set_text("原文")
        a.text.mark_set("insert","1.1")
        with patch.object(a,"focus_displayof",return_value=a.text):
            self.assertTrue(a.start_voice("rewrite"))
        with patch.object(a,"run_worker") as worker:
            self.emit("done","在这里插入新增")
            kind,token,work=worker.call_args.args
        a.cancel_operation()
        self.assertEqual(a.current_text(),"原文")
        a.set_text("取消后手工输入的新稿")
        a.events.put((kind,token,"ok",work()))
        self.pump()
        self.assertEqual(a.current_text(),"取消后手工输入的新稿")

    def test_instruction_shortcut_without_selection_starts_and_stops_caret_recording(self):
        a=self.app
        a.set_text("原文")
        a.text.mark_set("insert","1.1")
        with patch.object(a,"after",return_value="test-hold") as timer:
            self.assertEqual(a.key_press(SimpleNamespace(keysym="Control_L",state=0)),"break")
        self.assertEqual(a.hold_key,"Control")
        self.assertEqual(timer.call_args.args[0],250)
        begin=timer.call_args.args[1]
        with patch.object(a,"focus_displayof",return_value=a.text):
            begin()
        self.assertEqual(a.voice_mode,"rewrite")
        self.assertEqual((a.voice_start,a.voice_end),(1,1))
        self.assertEqual(a.active_hold,"Control")
        a.key_release(SimpleNamespace(keysym="Control_L",state=0))
        self.assertTrue(FakeVoice.sessions[-1].stopped)

    def test_stale_auto_result_does_not_overwrite_typing(self):
        a=self.app
        a.set_text("旧全文")
        token=(a.auto_request,a.revision,"旧全文")
        a.set_text("最新全文")
        a.events.put(("auto",token,"ok","旧模型返回"))
        self.pump()
        self.assertEqual(a.current_text(),"最新全文")

    def test_missing_key_and_selection_never_open_mic(self):
        a=self.app
        count=len(FakeVoice.sessions)
        a.settings.api_key=""
        self.assertFalse(a.start_voice("insert"))
        a.settings.api_key="sk-test"
        self.assertFalse(a.start_voice("replace"))
        self.assertEqual(len(FakeVoice.sessions),count)

    def test_first_microphone_permission_is_pending_without_recording_or_editing(self):
        a=self.app
        a.set_text("需要保留的当前草稿")
        self.select("1.0","1.4")
        original=a.current_text()
        history=(a.timeline.items.copy(),a.timeline.index)
        callbacks=[]
        self.permission.side_effect=lambda callback:callbacks.append(callback) or False
        self.assertFalse(a.start_voice("replace"))
        self.assertTrue(a._mic_permission_pending)
        self.assertFalse(a.busy)
        self.assertIsNone(a.voice)
        self.assertEqual(FakeVoice.sessions,[])
        self.assertEqual(a.current_text(),original)
        self.assertEqual((a.timeline.items,a.timeline.index),history)
        self.assertFalse(a.start_voice("replace"))
        self.assertEqual(len(callbacks),1)
        self.assertEqual(self.permission.call_count,1)

    def test_microphone_permission_approval_requires_a_new_recording_press(self):
        a=self.app
        a.set_text("原来的草稿")
        callbacks=[]
        self.permission.side_effect=lambda callback:callbacks.append(callback) or False
        self.assertFalse(a.start_voice("insert"))
        a.mic_key_down="Control"
        a.mic_key_pending=True
        callbacks[0](True,"已允许麦克风，请重新按住录音。")
        self.pump()
        self.assertFalse(a._mic_permission_pending)
        self.assertFalse(a.busy)
        self.assertEqual(FakeVoice.sessions,[])
        self.assertIsNone(a.mic_key_down)
        self.assertFalse(a.mic_key_pending)
        self.assertEqual(a.current_text(),"原来的草稿")
        self.assertIn("重新按住录音",a.status_var.get())
        self.permission.side_effect=None
        self.permission.return_value=True
        self.assertTrue(a.start_voice("insert"))
        self.assertEqual(len(FakeVoice.sessions),1)
        self.assertTrue(a.busy)

    def test_microphone_permission_denial_leaves_document_and_history_intact(self):
        a=self.app
        a.set_text("原文及历史不改变")
        history=(a.timeline.items.copy(),a.timeline.index)
        callbacks=[]
        self.permission.side_effect=lambda callback:callbacks.append(callback) or False
        self.assertFalse(a.start_voice("insert"))
        callbacks[0](False,"请在隐私与安全性中允许麦克风。")
        self.pump()
        self.assertFalse(a._mic_permission_pending)
        self.assertFalse(a.busy)
        self.assertEqual(FakeVoice.sessions,[])
        self.assertEqual(a.current_text(),"原文及历史不改变")
        self.assertEqual((a.timeline.items,a.timeline.index),history)
        self.assertIn("允许麦克风",a.status_var.get())

    def test_previous_microphone_permission_failure_displays_concrete_error(self):
        a=self.app
        a.set_text("原文")
        self.permission.side_effect=VoicePermissionError("Mac 未允许麦克风，请打开系统设置。")
        self.assertFalse(a.start_voice("insert"))
        self.assertFalse(a._mic_permission_pending)
        self.assertFalse(a.busy)
        self.assertEqual(FakeVoice.sessions,[])
        self.assertEqual(a.current_text(),"原文")
        self.assertEqual(a.status_var.get(),"Mac 未允许麦克风，请打开系统设置。")

    def test_stale_microphone_permission_callback_does_not_clear_new_request(self):
        a=self.app
        a.set_text("原文")
        callbacks=[]
        self.permission.side_effect=lambda callback:callbacks.append(callback) or False
        self.assertFalse(a.start_voice("insert"))
        callbacks[0](True,"第一次已允许，请重新录音。")
        self.pump()
        self.assertFalse(a._mic_permission_pending)
        self.assertFalse(a.start_voice("insert"))
        self.assertEqual(len(callbacks),2)
        status=a.status_var.get()
        callbacks[0](False,"旧的拒绝结果，不应覆盖。")
        self.pump()
        self.assertTrue(a._mic_permission_pending)
        self.assertEqual(a.status_var.get(),status)
        self.assertEqual(FakeVoice.sessions,[])
        callbacks[1](True,"新的授权已允许，请重新录音。")
        self.pump()
        self.assertFalse(a._mic_permission_pending)
        self.assertEqual(a.status_var.get(),"新的授权已允许，请重新录音。")

    def test_first_permission_during_space_hold_does_not_insert_space_on_release(self):
        a=self.app
        a.set_text("原文不能出现多余空格")
        a.text.mark_set("insert","end-1c")
        self.permission.return_value=False
        a.hold_key="space"
        a.hold_pending_space=True
        self.assertFalse(a.start_voice("insert"))
        self.assertIsNone(a.hold_key)
        self.assertFalse(a.hold_pending_space)
        a.key_release(SimpleNamespace(keysym="space",state=0))
        self.assertEqual(a.current_text(),"原文不能出现多余空格")
        self.assertEqual(a.key_press(SimpleNamespace(keysym="space",state=0)),"break")
        self.assertIsNone(a.hold_key)
        a.key_release(SimpleNamespace(keysym="space",state=0))
        self.assertEqual(a.current_text(),"原文不能出现多余空格")
        self.assertEqual(self.permission.call_count,1)
        self.assertEqual(FakeVoice.sessions,[])

    def test_history_preserves_whitespace_and_current_document(self):
        a=self.app
        a.set_text("旧历史  \n")
        a.save_current()
        a.set_text("当前草稿  ")
        a.history_list.selection_set(a.saved_history[0]["id"])
        a.preview_history()
        self.assertEqual(a.current_text(),"当前草稿  ")
        a.confirm_history()
        self.assertEqual(a.current_text(),"旧历史  \n")
        self.assertEqual(a.saved_history[0]["text"],"当前草稿  ")

    def test_emoji_cursor_and_live_range(self):
        a=self.app
        a.render_text("😀苹果",3,(1,3))
        self.assertEqual(a.text.get("1.0","insert"),"😀苹果")
        r=a.text.tag_ranges("live")
        self.assertEqual(a.text.get(r[0],r[1]),"苹果")

    def test_releasing_hold_after_focus_changes_stops_recording(self):
        a=self.app
        a.set_text("苹果")
        self.select("1.0","1.2")
        a.start_voice("replace")
        a.hold_key=a.active_hold="space"
        voice=FakeVoice.sessions[-1]
        a.key_release(SimpleNamespace(keysym="space"))
        self.assertTrue(voice.stopped)
        self.assertTrue(a.voice_stopping)
        self.assertIsNone(a.hold_key)

    def test_ctrl_chord_does_not_start_voice_and_active_hold_stops(self):
        a=self.app
        a.set_text("苹果")
        self.select("1.0","1.2")
        count=len(FakeVoice.sessions)
        a.key_press(SimpleNamespace(keysym="Control_L",state=0))
        a.key_press(SimpleNamespace(keysym="c",state=4))
        self.assertIsNone(a.hold_job)
        self.assertEqual(len(FakeVoice.sessions),count)
        a.start_voice("rewrite")
        a.hold_key=a.active_hold="Control_L"
        a.key_press(SimpleNamespace(keysym="c",state=4))
        self.assertTrue(FakeVoice.sessions[-1].stopped)

    def test_no_selection_allows_recording_and_caret_instruction_modification(self):
        a=self.app
        a.update_meta()
        self.assertFalse(a.replace_button.instate(["disabled"]))
        self.assertFalse(a.rewrite_button.instate(["disabled"]))
        self.assertIn("光标前后文字",a.selection_var.get())
        a.set_text("苹果")
        self.select("1.0","1.2")
        a.update_meta()
        self.assertFalse(a.replace_button.instate(["disabled"]))
        self.assertFalse(a.rewrite_button.instate(["disabled"]))

    def test_undo_blocks_pending_auto_result(self):
        a=self.app
        a.set_text("苹果")
        a.set_text("梨")
        token=(a.auto_request,a.revision,"梨")
        a.undo()
        a.events.put(("auto",token,"ok","过时整理"))
        self.pump()
        self.assertEqual(a.current_text(),"苹果")

    def test_buttons_fit_minimum_window_and_settings(self):
        a=self.app
        a.geometry("900x640")
        a.deiconify()
        a.update()
        def check_buttons(root):
            def walk(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from walk(child)
            for child in walk(root):
                if child.winfo_class() in ("TButton","TCheckbutton","Canvas") and child.winfo_ismapped():
                    if hasattr(a, 'settings_notebook') and str(child).startswith(str(a.settings_notebook)+'.'):
                        a.settings_viewport.see_focus(SimpleNamespace(widget=child))
                        root.update()
                    x=child.winfo_rootx()-root.winfo_rootx()
                    y=child.winfo_rooty()-root.winfo_rooty()
                    self.assertGreaterEqual(x,0,str(child))
                    self.assertGreaterEqual(y,0,str(child))
                    self.assertLessEqual(x+child.winfo_width(),root.winfo_width(),str(child))
                    self.assertLessEqual(y+child.winfo_height(),root.winfo_height(),str(child))
        check_buttons(a)
        a.open_settings()
        a.settings_window.geometry("650x660")
        a.settings_window.update()
        for tab in a.settings_tabs.values():
            a.settings_notebook.select(tab)
            a.settings_window.update()
            check_buttons(a.settings_window)
        controls = a.settings_tabs["controls"]
        a.settings_notebook.select(controls)
        a.settings_window.update()
        for child in controls.winfo_children():
            if child.winfo_class() == "TLabel" and child.winfo_ismapped():
                self.assertGreaterEqual(child.winfo_height(), child.winfo_reqheight(),
                                        str(child.cget("text")))
        a.settings_window.destroy()
        a.open_history()
        a.update()
        check_buttons(a.history_panel)
        a.close_history()

    def test_settings_pages_fit_scaled_fonts_at_their_measured_minimum(self):
        a = self.app
        original_scaling = float(a.tk.call("tk", "scaling"))
        a.deiconify()

        def check_controls(page, window):
            def walk(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from walk(child)
            for child in walk(page):
                if child.winfo_children() or not child.winfo_manager():
                    continue
                a.settings_viewport.see_focus(SimpleNamespace(widget=child))
                window.update_idletasks()
                self.assertTrue(child.winfo_ismapped(), str(child))
                self.assertGreaterEqual(child.winfo_height(), child.winfo_reqheight(), str(child))
                self.assertGreaterEqual(child.winfo_width(), child.winfo_reqwidth(), str(child))
                self.assertGreaterEqual(child.winfo_rootx(), window.winfo_rootx(), str(child))
                self.assertGreaterEqual(child.winfo_rooty(), window.winfo_rooty(), str(child))
                self.assertLessEqual(child.winfo_rootx() + child.winfo_width(),
                                     window.winfo_rootx() + window.winfo_width(), str(child))
                self.assertLessEqual(child.winfo_rooty() + child.winfo_height(),
                                     window.winfo_rooty() + window.winfo_height(), str(child))

        try:
            for scaling in (1.33333, 1.66667, 2.0):
                with self.subTest(scaling=scaling):
                    a.tk.call("tk", "scaling", scaling)
                    a.open_controls_settings()
                    window = a.settings_window
                    try:
                        window.update()
                        if scaling == 1.33333:
                            # macOS 窗口管理器可把请求尺寸限制在显示器可用区域。
                            self.assertGreaterEqual(window.winfo_width(), 650)
                            self.assertGreaterEqual(window.winfo_height(), 520)
                        min_width, min_height = window.minsize()
                        self.assertGreaterEqual(min_width, 650)
                        self.assertGreaterEqual(min_height, 520)
                        window.geometry(f"{min_width}x{min_height}")
                        window.update()
                        for page_name, page in a.settings_tabs.items():
                            a.settings_notebook.select(page)
                            window.update()
                            check_controls(page, window)
                            if page_name == "asr":
                                for provider in PROVIDERS.values():
                                    a.asr_provider_choice.set(provider.label)
                                    a.asr_provider_combobox.event_generate("<<ComboboxSelected>>")
                                    window.update()
                                    check_controls(page, window)
                    finally:
                        a._close_settings(window)
        finally:
            a.tk.call("tk", "scaling", original_scaling)

    def test_settings_size_is_stable_when_a_selected_page_has_not_been_laid_out(self):
        a = self.app
        a.deiconify()
        a.open_asr_settings()
        window = a.settings_window
        window.update()
        initial_size = (window.winfo_width(), window.winfo_height())
        initial_minimum = window.minsize()
        for page in a.settings_tabs.values():
            a.settings_notebook.select(page)
            with patch.object(page, "winfo_width", return_value=1), \
                 patch.object(page, "winfo_height", return_value=1):
                a._fit_settings_window(window)
            window.update()
            self.assertEqual(window.minsize(), initial_minimum)
            self.assertEqual((window.winfo_width(), window.winfo_height()), initial_size)
        window.geometry("900x900")
        window.update()
        expanded_size = (window.winfo_width(), window.winfo_height())
        for _ in range(5):
            a._fit_settings_window(window)
            window.update()
            self.assertEqual(window.minsize(), initial_minimum)
            self.assertEqual((window.winfo_width(), window.winfo_height()), expanded_size)

    def test_immediate_redo_after_typing_preserves_new_branch(self):
        a=self.app
        a.set_text("第一版")
        a.set_text("第二版")
        a.undo()
        a.text.insert("end","新写的")
        a.redo()
        self.assertEqual(a.current_text(),"第一版新写的")
        a.undo()
        self.assertEqual(a.current_text(),"第一版")

    def test_undo_redo_during_hold_do_not_leave_microphone_recording(self):
        for action in (self.app.undo,self.app.redo):
            a=self.app
            a.set_text("苹果")
            self.select("1.0","1.2")
            a.start_voice("rewrite")
            a.hold_key=a.active_hold="Control_L"
            voice=FakeVoice.sessions[-1]
            action()
            self.assertTrue(voice.stopped)
            a.cancel_operation()

    def test_auto_result_is_discarded_when_user_starts_selecting(self):
        a=self.app
        a.set_text("苹果")
        token=(a.auto_request,a.revision,"苹果")
        self.select("1.0","1.2")
        a.events.put(("auto",token,"ok","苹果。"))
        self.pump()
        self.assertEqual(a.current_text(),"苹果")
        self.assertEqual(a.get_selection()[2],"苹果")


if __name__=="__main__":
    unittest.main(verbosity=2)
