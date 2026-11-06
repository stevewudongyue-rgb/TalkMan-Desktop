"""语义路由、多轮澄清与确认事务；不联网、不操作外部 Agent。"""
import json
import unittest
from unittest.mock import Mock, patch

from talkman_commands import Command
from talkman_command_intent import Intent, interpret
from talkman_qwen import QwenError
import test_talkman_commands as command_tests


class SchemaTests(unittest.TestCase):
    def interpret(self, data):
        client = Mock()
        client._complete.return_value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return interpret(client, '停下以后再晚一点帮我整理')

    def test_valid_actions_edits_and_questions(self):
        self.assertEqual(self.interpret(dict(kind='action', action='delay', value=10, summary='等待十秒')).command,
                         Command('delay', 10))
        self.assertEqual(self.interpret(dict(kind='edit', instruction='把苹果改成梨')).instruction, '把苹果改成梨')
        self.assertEqual(self.interpret(dict(kind='ask', question='草稿还是历史？', options=['上一条草稿','上一条历史'])).options,
                         ('上一条草稿','上一条历史'))

    def test_unknown_actions_bad_types_and_extra_fields_never_execute(self):
        invalid = [('run_shell','anything'), ('restore_item',['delivery',{}]), ('delay',True),
                   ('delay',61), ('option',['自动标点','false']), ('option',['api_key',True]),
                   ('shortcut',['rewrite','Ctrl++J']), ('shortcut',['unknown','F7']),
                   ('position',0), ('position',False), ('scale',True), ('convention',['正确','']),
                   ('target','ChatGPT\n其他程序'), ('restore','older')]
        for action,value in invalid:
            with self.subTest(action=action,value=value), self.assertRaises(QwenError):
                self.interpret(dict(kind='action', action=action,value=value,summary='执行'))
        for data in ({'kind':'ask','question':'选一个','options':['a']*4},
                     {'kind':'edit','instruction':'修改','extra':'arbitrary'},
                     {'kind':'action','action':'copy','value':None,'summary':'复制','extra':1},
                     '{"kind":"action","action":"copy","action":"clear","value":null,"summary":"复制"}',
                     '```json\n{}\n```'):
            with self.subTest(data=data), self.assertRaises(QwenError):
                self.interpret(data)

    def test_model_only_sees_short_conversation_context(self):
        client=Mock()
        client._complete.return_value='{"kind":"ask","question":"草稿还是历史？","options":[]}'
        interpret(client,'刚才那段',[dict(role='user',content=str(i)) for i in range(20)],{'has_text':True})
        args=client._complete.call_args
        data=json.loads(args.args[0][-1]['content'])
        self.assertEqual([x['content'] for x in args.args[0][-9:-1]],[str(i) for i in range(12,20)])
        self.assertNotIn('document',data)
        self.assertTrue(args.kwargs['json_output'])


class SemanticUITests(unittest.TestCase):
    setUp = command_tests.CommandUITests.setUp
    tearDown = command_tests.CommandUITests.tearDown
    say = command_tests.CommandUITests.say

    def plan(self, phrase, intent):
        a=self.app
        client=Mock()
        client.interpret_command.return_value=intent
        with patch.object(a,'make_text_client',return_value=client), patch.object(a,'run_worker') as worker:
            self.say(phrase)
            kind,token,fn=worker.call_args.args
            self.assertEqual(kind,'command_plan')
            result=fn()
            self.commands.finish_semantic(token,'ok',result)
        return client

    def test_free_expression_confirms_actual_action_not_model_summary(self):
        a,c=self.app,self.commands
        self.plan('以后不要给我加那些逗号句号了',
                  Intent('action',Command('option',('自动标点',False)),summary='已删除所有正文'))
        self.assertTrue(a.punct_var.get())
        self.assertIn('关闭自动标点',a.status_var.get())
        self.assertNotIn('删除所有',a.status_var.get())
        self.plan('好，就这么办吧',Intent('action',Command('confirm'),summary='确认'))
        self.assertFalse(a.punct_var.get())
        self.assertFalse(c.active)
        self.assertEqual(a.current_text(),'这里是原正文。')

    def test_ambiguous_history_three_rounds_preserve_current_text(self):
        a,c=self.app,self.commands
        a.deliveries.add('昨天发出的正文','测试','测试对话')
        self.plan('刚才那段再放回来',Intent('ask',question='要恢复草稿还是发送历史？',options=('上一条草稿','上一条历史')))
        original=a.current_text()
        client=self.plan('上一条发出去的',Intent('action',Command('restore','delivery'),summary='恢复最近历史'))
        self.assertTrue(any('刚才那段' in x['content'] for x in client.interpret_command.call_args.args[1]))
        self.assertEqual(a.current_text(),original)
        self.assertTrue(c.pending)
        self.plan('行，替换吧',Intent('action',Command('confirm'),summary='确认恢复'))
        self.assertEqual(a.current_text(),'昨天发出的正文')
        self.assertTrue(any(x['text']==original for x in a.saved_history))

    def test_correction_replaces_old_plan(self):
        a,c=self.app,self.commands
        self.plan('那个加标点的先别开了',Intent('action',Command('option',('自动标点',False))))
        self.plan('不对，我想关的是排版',Intent('action',Command('option',('AI分行排版',False))))
        self.say('确认执行')
        self.assertTrue(a.punct_var.get())
        self.assertFalse(a.layout_var.get())

    def test_new_question_invalidates_old_pending_confirmation(self):
        a,c=self.app,self.commands
        self.plan('那个加标点的先别开了',Intent('action',Command('option',('自动标点',False))))
        self.plan('等等，不是那个',Intent('ask',question='要改哪一个开关？',options=('去语气词','分行排版')))
        self.say('确认执行')
        self.assertTrue(a.punct_var.get())
        self.assertIsNone(c.pending)
        self.assertIn('回答当前问题',a.status_var.get())

    def test_question_can_resolve_into_document_edit(self):
        a=self.app
        self.plan('改一下那个',Intent('ask',question='要改哪一处？'))
        with patch.object(a,'execute_text_instruction') as edit:
            self.plan('把这里的原正文改成新内容',Intent('edit',instruction='把原正文改成新内容'))
        edit.assert_called_once_with('把原正文改成新内容')

    def test_cancel_and_document_changes_reject_late_results(self):
        a,c=self.app,self.commands
        for reason in ('cancel','change'):
            client=Mock()
            with patch.object(a,'make_text_client',return_value=client),patch.object(a,'run_worker') as worker:
                self.say('帮我停止加标点吧')
                _,token,_=worker.call_args.args
                if reason=='cancel':a.cancel_operation()
                else:
                    a.render_text('新的正文',4)
                    a.revision+=1
                c.finish_semantic(token,'ok',Intent('action',Command('option',('自动标点',False))))
                self.assertFalse(a.busy)
                self.assertFalse(c.active)
                self.assertTrue(a.punct_var.get())
        self.assertEqual(a.current_text(),'新的正文')

    def test_failed_interpretation_keeps_document_and_offers_retry(self):
        a,c=self.app,self.commands
        with patch.object(a,'make_text_client',return_value=Mock()),patch.object(a,'run_worker') as worker:
            self.say('把那个调整一下')
            _,token,_=worker.call_args.args
            c.finish_semantic(token,'error','文字服务暂时繁忙，请再试一次。')
        self.assertEqual(a.current_text(),'这里是原正文。')
        self.assertTrue(c.clarification)
        self.assertFalse(a.busy)
        self.assertIsNotNone(c.panel)
        self.assertEqual(c.panel.winfo_toplevel(),a)

    def test_choices_use_same_semantic_route_without_microphone(self):
        a,c=self.app,self.commands
        self.plan('等一下再帮我整理',Intent('ask',question='等多久？',options=('停下后十秒','停下后三十秒')))
        with patch.object(a,'run_worker') as worker,patch.object(a,'make_text_client',return_value=Mock()),patch.object(a,'start_voice') as record:
            c.reply('停下后十秒')
            _,token,_=worker.call_args.args
            c.finish_semantic(token,'ok',Intent('action',Command('delay',10)))
            record.assert_not_called()
        self.assertNotEqual(a.auto_delay_var.get(),10)
        self.say('确认执行')
        self.assertEqual(a.auto_delay_var.get(),10)


if __name__=='__main__':unittest.main()
