"""编辑器到目标输入框的节流同步；所有原生调用交给已有目标线程。"""
from talkman_target import FillReceipt


class LiveSync:
    def __init__(self, app):
        self.app = app
        self.job = None
        self.epoch = 0
        self.inflight = False
        self.paused = False
        self.last = None
        self.sending = False

    def configure(self, enabled):
        self.app.pending_delivery = None
        self.epoch += 1
        self.cancel_timer()
        self.paused = False
        self.last = None
        if self.app.target_controller:
            self.app.target_controller.stop_sync()
        self.refresh()
        if enabled:
            self.changed()

    def cancel_timer(self):
        if self.job:
            self.app.after_cancel(self.job)
            self.job = None

    def changed(self):
        if getattr(getattr(self.app, 'app_commands', None), 'active', False):
            return
        if not self.app.sync_mode_var.get() or self.paused or self.app.closing:
            return
        if getattr(self.app, 'ai_review', None) is not None or getattr(self.app, 'convention_panel', None) is not None:
            return
        if getattr(getattr(self.app, 'position_picker', None), 'active', False):
            return
        if self.app.current_text() == self.last:
            return
        self.cancel_timer()
        self.job = self.app.after(350, self.flush)

    def flush(self, send=False):
        self.cancel_timer()
        if getattr(getattr(self.app, 'app_commands', None), 'active', False):
            return
        a = self.app
        if getattr(getattr(a, 'position_picker', None), 'active', False):
            return
        if not a.sync_mode_var.get() or self.paused or a.closing or self.inflight:
            return
        if getattr(a, 'ai_review', None) is not None or getattr(a, 'convention_panel', None) is not None or a.busy:
            if send:
                a.status_var.set('请先结束录音并确认改写预览，再下发。')
            return
        if send and (a.busy or a.auto_running):
            a.status_var.set('请等录音和文字整理结束，再下发。')
            return
        value = a.current_text()
        if send and not value.strip():
            a.status_var.set('预输入框为空。')
            return
        if not send and value == self.last:
            return
        if not a.target_controller:
            self.paused = True
            a.status_var.set('未连接目标，请在设置中选择后重新打开同步。')
            self.refresh()
            return
        epoch = self.epoch
        self.inflight, self.sending = True, send
        if send:
            a.cancel_auto_timer()
            a._delivery_snapshot = (a.revision, value)
            a.target_filling = True
            a.status_var.set('正在核对同步内容并向目标下发…')
        self.refresh()
        a.target_controller.sync(value, lambda state, result:
                                 a.events.put(('live_sync', (epoch, value, send), state, result)), send=send)

    def finish(self, token, state, result):
        epoch, text, send = token
        self.inflight = self.sending = False
        if send:
            self.app.target_filling = False
        if epoch != self.epoch or not self.app.sync_mode_var.get():
            if send:
                self.app._delivery_snapshot = None
            self.refresh()
            self.changed()
            return
        if state != 'ok':
            if send:
                self.app._delivery_snapshot = None
            self.paused = True
            self.app.status_var.set(str(result) + ' 可在设置中重新连接同步。')
            if send:
                self.app.button_feedback(self.app.target_button, '未完成')
        elif send:
            self.paused = True
            self.app.finish_target_fill(state, result)
            if isinstance(result, FillReceipt) and not self.app.delivery_error:
                self.app.status_var.set(self.app.status_var.get() + '；下一轮请重新连接同步。')
        else:
            self.last = text
            if self.app.current_text() != text:
                self.changed()
        self.refresh()

    def refresh(self):
        a = self.app
        enabled = a.sync_mode_var.get()
        for button in (a.target_button, a.compact_fill):
            a.set_button_label(button, '下发' if enabled or a.pending_delivery else '填入 Agent' if button == a.target_button else '填入')
            disabled = a.busy or a.target_filling or getattr(a, 'ai_review', None) is not None or getattr(a, 'convention_panel', None) is not None or getattr(getattr(a, 'position_picker', None), 'active', False) or (enabled and (self.inflight or self.paused or a.auto_running))
            button.state(['disabled'] if disabled else ['!disabled'])
        a.sync_status_var.set('同步已暂停' if enabled and self.paused else '同步中…' if enabled and self.inflight
                              else '实时同步 · 回车下发' if enabled else '')

    def close(self):
        self.cancel_timer()
        self.epoch += 1
        if self.app.target_controller:
            self.app.target_controller.stop_sync()
