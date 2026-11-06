"""同步只管理一个已验证输入框；每次写入前比较上次结果，不覆盖外部改动。"""
from talkman_target import TargetError


class SyncLease:
    def __init__(self, backend, target, text):
        self.backend, self.target = backend, target
        self.handle = backend.open_sync(target)
        current = backend.read_sync(target, self.handle)
        # 已有正文保留为前缀；本次会话仅管理自己的尾部，不反复追加。
        # 重新连接时，末尾已是当前正文则接管该尾部，避免重复。
        self.prefix = current[:-len(text)] if text and current.endswith(text) else current
        self.expected = current
        self.closed = False

    def update(self, text, current=lambda: True):
        if self.closed or not current():
            raise TargetError('同步已停止。')
        value = self.backend.read_sync(self.target, self.handle)
        if value != self.expected:
            self.closed = True
            raise TargetError('目标文字已被修改或发送，同步已暂停；两边文字都保留。')
        combined = self.prefix + text
        if combined != value:
            if not current():
                raise TargetError('同步已停止。')
            self.backend.write_sync(self.target, self.handle, combined, value, current)
            if self.backend.read_sync(self.target, self.handle) != combined:
                self.closed = True
                raise TargetError('目标未确认同步结果，已暂停；请检查目标输入框。')
        self.expected = combined

    def send(self, text, current=lambda: True):
        self.update(text, current)
        if not text.strip():
            raise TargetError('没有可下发的文字。')
        # 发送是不可重复动作，即使平台返回不明确，也不能自动重试。
        self.closed = True
        if not current():
            raise TargetError('同步已停止，本次未下发。')
        return self.backend.send_sync(self.target, self.handle, self.expected, current)
