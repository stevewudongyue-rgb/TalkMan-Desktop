"""不抢焦点的白话悬停说明；适用于按钮、开关和文字标签。"""
import tkinter as tk

BUTTON_HELP = {
    '设置': '在这里填写模型的 API Key、更换语音服务，或设置快捷键和窗口习惯。',
    '历史': '查看填入、下发或手动留存的文字、时间和目标；手动留存会单独标明。',
    '草稿簿': '保存还没发出去的想法。选一条先预览，确认后才放回编辑框。',
    '保存': '把当前文字存进草稿簿，之后可以找回来继续写。',
    '复制全部': '把编辑框里的全部文字复制到剪贴板，再到别的地方粘贴。',
    '上一步': '撤销上一次修改，让文字回到修改之前。',
    '下一步': '重新做回刚才撤销的那一步。',
    'AI 整理一次': '一次理顺全文的口误、标点和分行，尽量保持你的说话方式；设置中可开启确认预览。',
    '取消本次 · Esc': '取消当前录音或智能处理，恢复这次操作前的文字。',
    '录音': '没选字就接着录入；选中一段就替换它。再点一次结束。',
    '指令': '说出怎么改文字，或要 Talk Man 做什么，如“存个草稿”“把自动标点关了”。不清楚时会问你，可以继续回答。',
    '取消': '取消正在进行的录音或智能处理，保留原来的文字。',
    '收起': '把窗口收回屏幕边缘。还可以用全局快捷键叫出来录音。',
    '完整窗口 ↗': '回到完整编辑窗口，当前文字和录音会接着保留。',
    '导出 TXT': '把选中的记录另存为文字文件，方便分享或备份。',
    '外部编辑': '在电脑默认的文字编辑软件中打开这条记录。',
    '删除': '删除选中的记录。',
    '刷新': '重新查找已经打开的目标应用窗口。',
}

FIELD_HELP = {
    '千问通用 API Key': '从自己的千问账户复制连接凭证。语音和智能修改共用它；请不要把 Key 发给别人。',
    '服务地址': '填 Key 所属工作空间提供的服务地址。地址和 Key 不匹配时，模型会拒绝连接。',
    '文字模型': '负责按你的要求改字、去语气词和整理标点的 AI；不是语音识别模型。',
    '识别服务': '选择由哪家服务把声音转成文字。其他服务需要填写各自的凭证。',
    '识别方式': '录后识别会在录完后出字；实时转写会边说边出字，需要服务支持。',
    '录后识别模型': '录音结束后，由这个模型把声音变成文字。',
    '实时转写模型': '边说边出字时使用的模型。只能选择当前服务支持的型号。',
}


class HoverHelp:
    def __init__(self, app):
        self.app, self.job, self.window, self.owner = app, None, None, None
        self.text = None
        self.closed = False
        # 在控件事件之前收起说明；录音按钮会返回 break，不能只追加普通绑定。
        self.dismiss_tag = f'TalkManHelp{id(self)}'
        for sequence in ('<ButtonPress>', '<KeyPress>'):
            app.bind_class(self.dismiss_tag, sequence, lambda _e: self.hide())
        app.bind('<Unmap>', lambda e: self.hide() if e.widget == app else None, add=True)

    def attach(self, widget, text):
        widget.help_text = text
        if getattr(widget, '_help_bound', False):
            return
        widget._help_bound = True
        widget.bindtags((self.dismiss_tag,) + widget.bindtags())
        widget.bind('<Enter>', lambda _e: self.schedule(widget), add=True)
        widget.bind('<Leave>', lambda _e: self.hide_for(widget), add=True)
        widget.bind('<Destroy>', lambda e: self.hide_for(widget) if e.widget == widget else None, add=True)

    def schedule(self, widget):
        self.hide()
        if self.closed:
            return
        self.owner = widget
        self.job = self.app.after(650, self.show)

    def show(self):
        if self.job:
            self.app.after_cancel(self.job)
            self.job = None
        widget = self.owner
        if self.closed or widget is None or not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        guide = getattr(self.app, 'guide', None)
        if guide and guide.opened and guide.modal and widget.winfo_toplevel() == self.app:
            return
        content = widget.help_text() if callable(widget.help_text) else widget.help_text
        if not content:
            return
        self.text = content
        parent = widget.winfo_toplevel()
        # 使用所属窗口内的浮层，置顶／小窗时不会落到主窗后面，也不会抢系统焦点。
        win = self.window = tk.Frame(parent, takefocus=False)
        p = self.app.palette
        tk.Label(win, text=content, bg=p['input'], fg=p['text'], font=(self.app.fonts.ui, 11),
                 wraplength=max(120, min(310, parent.winfo_width()-48)), justify='left', padx=12, pady=10,
                 relief='solid', borderwidth=1).pack()
        win.update_idletasks()
        left, top = parent.winfo_rootx(), parent.winfo_rooty()
        right, bottom = left+parent.winfo_width(), top+parent.winfo_height()
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(left, min(widget.winfo_rootx(), right-width))
        y = widget.winfo_rooty()+widget.winfo_height()+6
        if y+height > bottom:
            y = widget.winfo_rooty()-height-6
        y = max(top, min(y, bottom-height))
        win.place(x=x-left, y=y-top)
        win.lift()

    def hide_for(self, widget):
        if self.owner == widget:
            self.hide()

    def hide(self):
        if self.job:
            self.app.after_cancel(self.job)
            self.job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.owner = None

    def close(self):
        self.closed = True
        self.hide()
