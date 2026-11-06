"""指令操作复用现有动作；待确认内容留在原窗口，可继续用语音确认。"""
import time
import re
import tkinter as tk
from tkinter import ttk

from talkman_commands import Command, OPTIONS, parse_command, compact
from talkman_conventions import convention_key, is_remember_instruction
from talkman_input_positions import navigation_instruction
from talkman_shortcuts import ACTION_LABELS, save_shortcuts, validate_shortcuts, display_shortcut
from talkman_translation import LANGUAGES


HELP = ('可以说：保存到草稿簿；把上一条历史放入编辑框；打开自动标点；关闭 AI 分行排版；'
        '把指令快捷键改成 Ctrl 加 Alt 加 J；记住约定词 Colo Play，不是可乐派；'
        '选择输入位置；下一个输入框。\n需要确认时再说“确认执行”或“取消操作”。改正文仍可说“把苹果改成梨”。')


class AppCommands:
    def __init__(self, app):
        self.app = app
        self.pending = None
        self.panel = None
        self.generation = 0
        self.dialogue = []
        self.clarification = False
        self.planning = False
        self.dialogue_anchor = None
        self.dialogue_deadline = 0

    @property
    def active(self):
        return self.pending is not None or self.clarification or self.planning

    def recognizes(self, text):
        command = parse_command(text)
        if command and command.action == 'unsupported' and '密钥' not in str(command.value) and self.app.text_model_ready():
            command = None
        return bool(command or navigation_instruction(text) or is_remember_instruction(text))

    def handle(self, text):
        a = self.app
        command = parse_command(text)
        if command and command.action == 'confirm':
            if self.pending:
                return self.confirm()
            if self.clarification:
                a.status_var.set('请先回答当前问题，再确认要做的操作。')
                return True
            self.cancel()
            if a.ai_review is not None:
                a.ai_review_finish(True)
            elif a.convention_panel is not None:
                a.term_save()
            else:
                a.status_var.set('目前没有等待确认的应用操作。')
            return True
        if command and command.action == 'cancel':
            self.cancel()
            a.position_picker.cancel()
            a.cancel_operation()
            a.status_var.set('已取消待确认操作。')
            return True
        if a.ai_review is not None or a.convention_panel is not None:
            a.status_var.set('请先说“确认执行”或“取消操作”，完成当前预览。')
            return True
        if navigation_instruction(text):
            self.cancel()
            return a.position_picker.instruction(text)
        if is_remember_instruction(text):
            self.cancel()
            a.remember_selection()
            return True
        if command is None:
            if self.active:
                a.status_var.set('有操作等待确认。请说“确认执行”或“取消操作”，再修改正文。')
                return True
            return False
        if a.busy or a.target_filling:
            a.status_var.set('请等当前操作完成，再下应用指令。')
            return True
        self.cancel()
        try:
            self.execute(command)
        except (OSError, ValueError) as error:
            a.status_var.set(str(error) if isinstance(error, ValueError) else '操作未能保存，请检查数据目录。')
        return True

    def snapshot(self):
        a = self.app
        target = getattr(a.target_controller, 'selected', None)
        selection = a.get_selection()
        position = selection[:2] if selection else a.text.index('insert')
        return a.revision, a.current_text(), target, a.pending_delivery, position

    def request(self, command, message):
        a = self.app
        a.cancel_auto_timer()
        a.live_sync.cancel_timer()
        a.window_controller.reveal(focus=False)
        a.window_controller.keep_expanded()
        self.pending = (command, self.snapshot(), time.monotonic()+180)
        self.dialogue = [*self.dialogue[-6:], {'role':'assistant','content':'等待确认：'+message}]
        self.dialogue_anchor, self.dialogue_deadline = self.snapshot(), time.monotonic()+180
        self.show_panel(message, confirmation=True)

    def show_panel(self, message, confirmation=False, options=()):
        a = self.app
        self.remove_panel()
        a.window_controller.reveal(focus=False)
        a.window_controller.keep_expanded()
        panel = self.panel = ttk.Frame(a, style='Panel.TFrame', padding=16, borderwidth=1, relief='solid')
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        ttk.Label(panel, text='指令 · 等待确认' if confirmation else '指令 · 请补充一句',
                  style='PanelMuted.TLabel').grid(row=0, column=0, sticky='w', pady=(0,8))
        viewport = ttk.Frame(panel, style='Panel.TFrame')
        viewport.grid(row=1, column=0, sticky='nsew')
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        canvas = tk.Canvas(viewport, bg=a.palette['panel'], highlightthickness=0, height=150)
        canvas.grid(row=0,column=0,sticky='nsew')
        scroll = ttk.Scrollbar(viewport, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        content = ttk.Frame(canvas,style='Panel.TFrame')
        item = canvas.create_window((0,0),window=content,anchor='nw')
        label = ttk.Label(content,text=message,style='Panel.TLabel',wraplength=500)
        label.pack(fill='x',pady=(0,12))
        def resize(event=None):
            width=max(100,canvas.winfo_width())
            canvas.itemconfigure(item,width=width)
            label.configure(wraplength=max(80,width-8))
            canvas.configure(scrollregion=canvas.bbox('all'))
            if content.winfo_reqheight()>canvas.winfo_height()+2:
                scroll.grid(row=0,column=1,sticky='ns',padx=(6,0))
            else:
                scroll.grid_remove()
        canvas.bind('<Configure>',resize)
        content.bind('<Configure>',resize)
        def wheel(event):
            if content.winfo_reqheight()>canvas.winfo_height():
                canvas.yview_scroll(-1 if event.delta>0 else 1,'units')
            return 'break'
        canvas.bind('<MouseWheel>',wheel)
        content.bind('<MouseWheel>',wheel)
        label.bind('<MouseWheel>',wheel)
        row = ttk.Frame(panel, style='Panel.TFrame')
        for option in options:
            button=a.button(content, option, lambda reply=option: self.reply(reply), style='Outline.TButton')
            button.pack(fill='x',pady=(0,6))
            button.bind('<MouseWheel>',wheel)
        row.grid(row=2,column=0,sticky='ew',pady=(8,0))
        a.button(row, '取消', a.cancel_operation, style='Ghost.TButton').pack(side='right')
        if confirmation:
            a.button(row, '确认执行', self.confirm, style='Accent.TButton').pack(side='right', padx=8)
        a.button(row, '继续说 / 停止', a.toggle_command, style='Outline.TButton').pack(side='left')
        panel.place(in_=a.editor_body, relx=.5, rely=.02, anchor='n', relwidth=.94, relheight=.96)
        panel.lift()
        a.status_var.set(message + (' · 可说“确认执行”或“取消操作”。' if confirmation else ' · 直接按指令继续回答。'))

    def remove_panel(self):
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None

    def cancel(self):
        self.generation += 1
        self.pending = None
        self.dialogue = []
        self.clarification = self.planning = False
        self.dialogue_anchor = None
        self.remove_panel()

    def begin_semantic(self, text, client):
        a = self.app
        if self.dialogue_anchor is not None and (self.snapshot()!=self.dialogue_anchor or time.monotonic()>self.dialogue_deadline):
            self.cancel()
            a.restore_voice('上一轮的正文、目标或时间已变化，请重新完整说明要做什么。')
            return
        context = dict(has_text=bool(a.current_text()), has_selection=bool(a.get_selection()),
                       selected_word=(a.get_selection()[2][:80] if a.get_selection() else ''),
                       pending_confirmation=bool(self.pending or a.ai_review is not None or a.convention_panel is not None),
                       has_delivery=bool(a.pending_delivery), choosing_position=a.position_picker.active)
        messages = self.dialogue[-8:]
        self.dialogue = [*messages, {'role':'user','content':text}]
        self.planning = True
        self.remove_panel()
        a.refresh_cancel_button()
        token = (self.generation, a.operation_id, self.snapshot())
        a.status_var.set('正在理解这条指令…')
        a.run_worker('command_plan', token, lambda: client.interpret_command(text, messages, context))

    def finish_semantic(self, token, state, intent):
        a = self.app
        generation, operation, snapshot = token
        if generation != self.generation or operation != a.operation_id or a.closing:
            return
        self.planning = False
        if snapshot != self.snapshot():
            self.cancel()
            a.set_busy(False)
            a.status_var.set('理解指令期间正文或目标已变化，本次未执行。')
            return
        if state == 'ok' and intent.kind == 'edit' and not (self.pending or a.ai_review is not None or a.convention_panel is not None):
            self.cancel()
            a.execute_text_instruction(intent.instruction)
            return
        a.restore_voice('已理解指令，尚未执行。')
        if a.ai_review is not None:
            a.text.configure(state='disabled')
        if state != 'ok' or intent.kind in ('ask', 'edit'):
            question = str(intent) if state != 'ok' else intent.question if intent.kind == 'ask' else '当前有操作等待确认。请先说“取消操作”，再说要怎样修改正文。'
            # 新的疑问或理解失败使旧预案失效，不能对新问题说“确认”却执行旧动作。
            self.pending = None
            self.clarification = True
            self.dialogue = [*self.dialogue[-6:], {'role':'assistant','content':question}]
            self.dialogue_anchor, self.dialogue_deadline = self.snapshot(), time.monotonic()+180
            self.show_panel(question, options=intent.options if state=='ok' and intent.kind=='ask' else ())
            return
        command = intent.command
        if command.action in ('confirm','cancel'):
            self.handle('确认执行' if command.action=='confirm' else '取消操作')
            return
        self.cancel()
        if a.ai_review is not None or a.convention_panel is not None:
            a.status_var.set('请先确认或取消当前预览，再执行其他操作。')
            return
        try:
            # 已有完整预览的动作走原流程；其他模型理解的修改显示真实动作，
            # 不使用模型 summary 冒充执行结果或确认文案。
            if command.action in ('restore','convention','priority_word','speaker_name','shortcut','clear','send','target','position',
                                  'settings','drafts','history','close_records','close_settings','help'):
                self.execute(command)
            else:
                self.request(command, self.describe(command))
        except (OSError,ValueError) as error:
            a.status_var.set(str(error) if isinstance(error,ValueError) else '操作未能保存，原内容保留。')

    def reply(self, text):
        a = self.app
        if a.busy or a.target_filling:
            return
        if self.recognizes(text):
            self.handle(text)
            return
        if not a.text_model_ready():
            a.status_var.set('灵活表达需要可用的文字模型，可在设置中连接。')
            return
        a.capture_edit()
        a.cancel_auto_timer()
        a.voice_base = a.current_text()
        selected = a.get_selection()
        a.voice_start = len(a.text.get('1.0', selected[0] if selected else 'insert'))
        a.voice_end = len(a.text.get('1.0', selected[1])) if selected else a.voice_start
        a.voice_selection = (a.voice_start,a.voice_end) if selected else None
        a.voice_conventions = a.convention_snapshot()
        a.voice_mode = 'rewrite'
        a.operation_id += 1
        a.set_busy(True)
        self.begin_semantic(text, a.make_text_client())

    @staticmethod
    def describe(command):
        action, value = command.action, command.value
        if action == 'option':
            return ('打开' if value[1] else '关闭')+value[0]+'？'
        descriptions = {'save':'把当前正文保存到草稿簿','manual_history':'把当前正文手动留存到历史（不会发送）',
            'copy':'复制全部正文','undo':'撤销上一次编辑','redo':'重做上一次编辑','select_all':'全选正文',
            'deselect':'取消选区','organize':'让 AI 整理一次全文','translate':'翻译当前选中文字',
            'fill':'把当前正文填入已选择的目标（不会发送）','expand':'展开主窗口',
            'delay':f'把自动整理延迟设为 {value} 秒','language':f'把翻译目标设为 {value}',
            'theme':'切换为'+{'dark':'深色','light':'浅色','swiss':'瑞士国际'}.get(str(value),'')+'主题',
            'dock':f'把窗口收起到 '+{'left':'左边','right':'右边','top':'顶部'}.get(str(value),''),
            'gesture':'设置录音方式：'+{'hold':'按住录音','toggle':'点按启停','both':'点按和按住都支持'}.get(str(value),''),
            'scale':f'把界面字号倍率设为 {value}（重启生效）', 'code_language':'切换口述场景为 '+{'off':'普通口述','python':'Python','javascript':'JavaScript','sql':'SQL'}.get(str(value),'')}
        return descriptions.get(action, action)+'？'

    def confirm(self):
        a = self.app
        if a.busy or a.target_filling:
            a.status_var.set('请等录音或填入完成后确认。')
            return True
        pending = self.pending
        self.cancel()
        if not pending:
            return True
        command, snapshot, deadline = pending
        if self.snapshot() != snapshot or time.monotonic() > deadline:
            a.status_var.set('正文、目标或确认时间已变化，请重新下指令。')
            return True
        try:
            self.execute(command, confirmed=True)
        except (OSError, ValueError) as error:
            a.status_var.set(str(error) if isinstance(error, ValueError) else '操作未能保存，原内容保留。')
        return True

    def execute(self, command, confirmed=False):
        a = self.app
        action, value = command.action, command.value
        if action == 'unsupported':
            a.status_var.set(value)
        elif action == 'help':
            self.request(Command('dismiss'), HELP)
        elif action == 'dismiss':
            a.status_var.set('可随时通过指令按钮操作 Talk Man。')
        elif action == 'save':
            a.save_current()
        elif action == 'manual_history':
            if a.require_feature('records') and a.current_text().strip():
                a.deliveries.add(a.current_text(), 'Talk Man', '手动记录', status='手动留存')
                a.refresh_history()
                a.status_var.set('已记录到历史 · 手动留存，未填入或发送。')
        elif action in ('drafts', 'history'):
            a.open_history('delivery' if action == 'history' else 'notebook')
        elif action == 'close_records':
            a.close_history()
        elif action == 'restore':
            if not a.require_feature('records'):
                return
            items = a.deliveries.items if value == 'delivery' else a.saved_history
            if not items:
                a.status_var.set('还没有可恢复的历史。' if value == 'delivery' else '草稿簿里还没有内容。')
                return
            item = dict(items[0])
            a.open_history(value)
            a.history_list.selection_set(item['id'])
            a.preview_history()
            self.request(Command('restore_item', (value, item)),
                f"恢复 {item['time']} 的{'历史' if value == 'delivery' else '草稿'}？当前正文先存入草稿簿。")
        elif action == 'restore_item' and confirmed:
            if not a.require_feature('records'):
                return
            mode, item = value
            items = a.deliveries.items if mode == 'delivery' else a.saved_history
            if not any(x['id'] == item['id'] and x['text'] == item['text'] for x in items):
                raise ValueError('这条记录已删除或更改，请重新选择。')
            if a.save_current():
                a.translation_memory.restore_draft(item['text'], item.get('translation_protected'))
                a.set_text(item['text'])
                a.draft_id = item['id'] if mode == 'notebook' else None
                a.close_history()
                a.status_var.set('已恢复记录，原正文已存入草稿簿。')
        elif action == 'option':
            label, enabled = value
            key, variable = OPTIONS[label]
            if key == 'sync_mode':
                if enabled and not confirmed:
                    self.request(command, '打开同步后，当前正文会实时写入选定的目标输入框。')
                    return
                a.set_sync_mode(enabled)
                return
            if key == 'free_mode' and enabled and not a.free_service.configured:
                raise ValueError('这台电脑尚未开通免费服务，请先在设置中检查连接。')
            if key == 'speaker_enabled' and enabled:
                from talkman_expression import validate_speaker_name
                validate_speaker_name(a.speaker_name_var.get())
            a.save_ui_preferences(**{key:enabled})
            getattr(a, variable).set(enabled)
            if key == 'speaker_enabled' and hasattr(a, 'speaker_enabled_input'):
                a.speaker_enabled_input.set(enabled)
            if key.startswith('auto_') or key == 'direct_request':
                a.auto_request += 1
                a.auto_running = False
                a.refresh_cancel_button()
                a.schedule_auto()
            if key in ('topmost', 'edge_hide', 'global_voice'):
                a.window_controller.apply_preferences()
            if key == 'magnifier':
                a.magnifier.hide()
            if key == 'free_mode':
                a.provider_var.set(a._provider_label())
            a.status_var.set(('已打开' if enabled else '已关闭') + label)
        elif action == 'speaker_name':
            from talkman_expression import validate_speaker_name
            name = validate_speaker_name(value)
            if not confirmed:
                self.request(command, f'之后的新录入用「{name}」代替本人“我”？已有文字不变。')
                return
            a.save_ui_preferences(speaker_name=name, speaker_enabled=True)
            a.speaker_name_var.set(name)
            a.speaker_enabled_var.set(True)
            if hasattr(a, 'speaker_name_input'):
                a.speaker_name_input.set(name)
                a.speaker_enabled_input.set(True)
                a.speaker_feedback.set(f'已通过指令保存角色：{name}')
            a.status_var.set(f'已保存角色：{name}，之后的新录入生效。')
        elif action == 'shortcut':
            key, shortcut = value
            mapping = validate_shortcuts({**a.shortcuts, key:shortcut})
            if not confirmed:
                self.request(command, f'把{ACTION_LABELS[key]}的快捷键改成 {display_shortcut(mapping[key])}？')
                return
            a.shortcuts = save_shortcuts(a.data_dir, mapping)
            a.cancel_hold()
            a.mic_key_down = None
            a.mic_key_pending = False
            a.refresh_shortcut_labels()
            a.window_controller.sync_hotkeys()
            win = getattr(a, 'settings_window', None)
            if win and win.winfo_exists():
                a._close_settings(win)
            a.status_var.set(f'已保存{ACTION_LABELS[key]}快捷键：{display_shortcut(mapping[key])}')
        elif action == 'delay':
            if not 1 <= value <= 60:
                raise ValueError('自动整理延迟支持 1—60 秒。')
            a.save_ui_preferences(auto_delay=value)
            a.auto_delay_var.set(value)
            a.schedule_auto()
            a.status_var.set(f'已设置：停下输入 {value} 秒后自动整理。')
        elif action == 'gesture':
            a.save_ui_preferences(voice_gesture=value)
            a.voice_gesture_var.set(value)
            a.status_var.set('录音方式已设为'+{'hold':'按住录音，松开结束','toggle':'点一下开始，再点结束','both':'支持点按和按住'}[value])
        elif action == 'scale':
            a.save_ui_preferences(ui_scale=value)
            a.status_var.set(f'字号已设为 {round(value*100)}%，重新打开应用后生效。')
        elif action == 'code_language':
            if a.require_feature('conventions'):
                a.conventions.set_language(value)
                a.conventions_changed()
                a.status_var.set('已切换口述场景：'+ ('普通口述' if value == 'off' else value))
        elif action == 'language':
            if not a.require_feature('translation'):
                return
            if value not in LANGUAGES:
                raise ValueError('请使用支持的语言：'+'、'.join(LANGUAGES))
            a.save_ui_preferences(translation_language=value)
            a.translation_language.set(value)
            a.translation_language_changed()
            a.status_var.set('翻译目标语言已设为'+value)
        elif action == 'priority_word':
            if not a.require_feature('conventions'):
                return
            if not confirmed:
                self.request(command, f'把「{value}」记为优先词？AI 整理时参考，不需要填写误写。')
                return
            item = next((x for x in a.conventions.items if convention_key(x['word']) == convention_key(value)), None)
            a.conventions.put(value, item['aliases'] if item else [], item['scope'] if item else 'all', True,
                              item['id'] if item else None, priority=True)
            a.conventions_changed()
            a.status_var.set(f'已保存优先词「{value}」，下次 AI 整理时参考。')
        elif action == 'convention':
            if not a.require_feature('conventions'):
                return
            word, alias = value
            if not confirmed:
                self.request(command, f'记住正确写法「{word}」，把「{alias}」作为误写？')
                return
            item = next((x for x in a.conventions.items if convention_key(x['word']) == convention_key(word)), None)
            aliases = [*(item['aliases'] if item else []), *re.split('[,，、;；]', alias)]
            a.conventions.put(word, aliases, item['scope'] if item else 'all', True, item['id'] if item else None)
            a.conventions_changed()
            a.status_var.set(f'已记住「{word}」，下次录音生效。')
        elif action in ('clear', 'send'):
            if not confirmed:
                if action == 'send' and not a.pending_delivery and not a.sync_mode_var.get():
                    raise ValueError('请先说“填入 Agent”，核对后再说“下发”。')
                self.request(command, '清空整个编辑区？' if action == 'clear' else '向已核对的输入框发送文字？')
            elif action == 'clear':
                a.clear_text()
            else:
                a.fill_to_target()
        elif action == 'fill':
            if a.pending_delivery or a.sync_mode_var.get():
                raise ValueError('当前已有待下发文字。要发送请说“下发”，不会重复填入。')
            a.fill_to_target()
        elif action == 'settings':
            a.open_settings(value)
        elif action == 'target':
            generation = self.generation
            a.status_var.set('正在查找目标窗口…')
            def done(state, rows):
                if generation != self.generation or a.closing:
                    return
                if state != 'ok':
                    a.status_var.set(str(rows))
                    return
                choices = [row for row in rows if compact(value) in
                           (compact(row['app']), compact(row['title']), compact(row['label']))]
                if len(choices) != 1:
                    a.status_var.set('找到多个同名窗口，请说完整的窗口标题，或在目标列表中选择。' if choices
                                     else '没有找到这个目标，请先打开应用，或说目标列表中的完整名称。')
                    return
                target = a.target_windows.get(choices[0]['id'])
                if target is not None:
                    a.position_picker.cancel()
                    a.position_picker.open(target)
            a.refresh_target_windows(done)
        elif action == 'position':
            if value == 'begin':
                a.position_picker.open()
            elif value == 'cancel':
                a.position_picker.cancel()
            elif a.position_picker.active:
                a.position_picker.action('index', value-1) if type(value) is int else a.position_picker.action(value)
            else:
                a.status_var.set('请先说“选择输入位置”，再选择编号。')
        elif action == 'close_settings':
            win = getattr(a, 'settings_window', None)
            if win and win.winfo_exists():
                a._close_settings(win)
        elif action == 'theme':
            if a.theme_key != value:
                a.set_theme(value)
        elif action == 'dock':
            a.window_controller.dock(value)
        elif action == 'expand':
            a.window_controller.reveal(focus=True)
        elif action in ('select_all', 'deselect'):
            a.text.tag_remove('sel', '1.0', 'end')
            if action == 'select_all':
                a.text.tag_add('sel', '1.0', 'end-1c')
            a.update_meta()
        elif action in ('copy', 'undo', 'redo', 'organize', 'translate'):
            {'copy':a.copy_all, 'undo':a.undo, 'redo':a.redo, 'organize':a.organize_once,
             'translate':a.translate_document}[action]()
