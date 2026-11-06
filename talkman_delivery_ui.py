"""普通填入的二次下发确认；目标线程持有实际输入框快照。"""
import time
from talkman_target import FillReceipt


class DeliveryUIMixin:
    def disarm_delivery(self):
        self.pending_delivery = None
        if hasattr(self, 'live_sync'):
            self.live_sync.refresh()

    def check_delivery_confirmation(self):
        pending = self.pending_delivery
        if pending and (time.monotonic() >= pending['expires'] or pending['revision'] != self.revision
                        or pending['text'] != self.current_text() or self.sync_mode_var.get()):
            self.disarm_delivery()

    def finish_normal_fill(self, state, receipt):
        snapshot = self._delivery_snapshot
        cleared = self.finish_target_fill(state, receipt)
        if (state == 'ok' and isinstance(receipt, FillReceipt) and receipt.send_token
                and not self.sync_mode_var.get()
                and (cleared or snapshot == (self.revision, self.current_text()))):
            self.pending_delivery = dict(token=receipt.send_token, expires=time.monotonic()+60,
                revision=self.revision, text=self.current_text(), ready_at=time.monotonic()+.6)
            # 二次确认状态立即可见，不被“已填入”短暂反馈覆盖。
            for button in (self.target_button, self.compact_fill):
                feedback = self.feedback_jobs.pop(button, None)
                if feedback:
                    self.after_cancel(feedback[0])
            self.status_var.set('已填入，尚未发送；60 秒内再点「下发」向该输入框按回车。')
        self.live_sync.refresh()

    def send_pending_delivery(self):
        self.check_delivery_confirmation()
        pending = self.pending_delivery
        if not pending:
            self.status_var.set('这次确认已失效，请检查目标输入框。')
            return
        if time.monotonic() < pending['ready_at']:
            self.status_var.set('已填入，尚未发送。请检查内容，再点一次「下发」。')
            return
        self.disarm_delivery()
        self.cancel_auto_timer()
        self.auto_request += 1
        self.auto_running = False
        self.target_filling = True
        self._delivery_snapshot = None
        self.status_var.set('正在核对刚填入的内容并下发…')
        self.live_sync.refresh()
        self.target_controller.send_filled(pending['token'], lambda state, value:
            self.events.put(('target_send', None, state, value)))
