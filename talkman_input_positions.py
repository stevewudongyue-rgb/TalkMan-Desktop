"""输入位置选择事务。原生控件只留在目标 worker，UI 只接收编号和矩形。"""
import re
import uuid


def main_input_role(target, identifier, labels):
    """只认可明确的聊天输入语义；不以坐标、序号或泛称 editor 猜测。"""
    from talkman_target import is_agent_window
    if not is_agent_window(target):
        return None
    identifier = str(identifier or '').strip().casefold()
    if identifier == 'prompt-textarea':
        return ('id', identifier)
    known = {'message chatgpt', 'message claude', 'message deepseek', 'message kimi',
             'message copilot', '发送消息', '发消息', '输入消息', '消息输入框',
             '给豆包发送消息', '与豆包聊天', '向 deepseek 发送消息', '随便问，尽管聊'}
    for label in labels:
        value = str(label or '').strip().rstrip('…。.').casefold()
        if value in known:
            return ('label', value)
    return None


def navigation_instruction(text):
    """仅识别明确的导航短句，不将普通改写指令猜成导航。"""
    text = re.sub(r'[\s，。！？,.!?]', '', str(text))
    if text in ('选择输入位置', '选择输入框', '切换输入位置', '切换输入框'):
        return 'begin', None
    if text in ('下一个输入框', '切换到下一个输入框', '下一个输入位置'):
        return 'next', None
    if text in ('上一个输入框', '切换到上一个输入框', '上一个输入位置'):
        return 'previous', None
    if text in ('确认这个位置', '确认输入位置', '确认这个输入框'):
        return 'confirm', None
    if text in ('取消位置选择', '取消输入框选择', '取消输入位置选择'):
        return 'cancel', None
    match = re.fullmatch(r'(?:选择|切换到|选中)第([一二三四五六七八九十两\d]+)个输入(?:框|位置)', text)
    if match:
        value = match[1]
        numbers = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                   '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        number = int(value) if value.isascii() and value.isdigit() else numbers.get(value, 0)
        if 1 <= number <= 30:
            return 'index', number - 1
    return None


class InputPositionsMixin:
    def _init_input_positions(self):
        self._position_generation = 0
        self._position_active = False
        self._position_preview = None
        self._position_bindings = {}
        self._position_roles = {}
        self._position_labels = {}
        self._position_token = None
        self._position_label = ('主输入框' if self.selection.metadata.get('input_role') else
                                '已记住位置' if self.selection.metadata.get('input_mode') == 'explicit'
                                else '输入位置')

    @property
    def position_label(self):
        with self._lock:
            return self._position_label

    @property
    def choosing_position(self):
        with self._lock:
            return self._position_active

    def begin_positions(self, callback, target=None):
        with self._lock:
            self._position_generation += 1
            self._position_active = True
            self._sync_epoch += 1
            self._pending_send = None
            self._queue.put(('positions', callback, ('begin', self._position_generation, target)))

    def position_action(self, action, callback=None, index=None):
        with self._lock:
            if action == 'cancel':
                self._position_generation += 1
                self._position_active = False
            self._queue.put(('positions', callback, (action, self._position_generation, index)))

    def _position_reset(self):
        # 只能在 select/clear 的锁内调用；不在 UI 线程释放原生对象。
        self._position_generation += 1
        self._position_active = False
        self._position_token = None
        self._position_label = '输入位置'

    @staticmethod
    def _position_same_window(left, right):
        return (left.session_key() == right.session_key()
                and left.identity == right.identity
                and left.process_started == right.process_started)

    def _position_cached(self, target, *, main=False):
        for token, (original, _element) in reversed(list(self._position_bindings.items())):
            if self._position_same_window(original, target):
                if (main and self._position_roles.get(token)) or (not main and original.title == target.title):
                    return token
        return None

    def _position_restore(self, target):
        # UI 线程只取已有 token，不调用／释放原生控件。具体可用性在 worker 填入前验证。
        token = self._position_cached(target)
        preference = self.selection.position_for(target)
        if token is None and preference is None:
            token = self._position_cached(target, main=True)
        self._position_token = token
        if token:
            role = self._position_roles.get(token)
            self.selection.metadata = dict(target.metadata(), input_mode='explicit')
            if role:
                self.selection.metadata['input_role'] = list(role)
            self._position_label = self._position_labels[token]
        elif self.selection.metadata.get('input_role'):
            self._position_label = '主输入框'
        elif preference:
            self._position_label = '已记住位置'

    def _position_remember_live(self, target, element, role, label):
        # 仅 worker 在锁内调用。保留其他对话的位置，最多 24 个，避免一直持有 AX／COM 对象。
        token = self._position_cached(target) or uuid.uuid4().hex
        self._position_bindings.pop(token, None)
        self._position_bindings[token] = (target, element)
        self._position_roles[token] = role
        self._position_labels[token] = label
        while len(self._position_bindings) > 24:
            oldest = next(iter(self._position_bindings))
            self._position_bindings.pop(oldest)
            self._position_roles.pop(oldest, None)
            self._position_labels.pop(oldest, None)
        self._position_token = token
        self._position_label = label
        return token

    def _position_sync_backend(self, target, *, recover=False, epoch=None):
        from talkman_target import PositionRelocationRequired, TargetError
        with self._lock:
            if self._position_active:
                raise TargetError('请先确认或取消输入位置选择。')
            token = self._position_token
            required = self.selection.metadata.get('input_mode') == 'explicit'
            role = self._position_roles.get(token) if token else self.selection.metadata.get('input_role')
            binding = self._position_bindings.get(token) if token else None
            if binding and not self._position_same_window(binding[0], target):
                raise TargetError('目标窗口进程已变化，请重新选择目标窗口。')
            auto_main = False
            if recover:
                cached = self._position_cached(target)
                preference = self.selection.position_for(target)
                if cached:
                    token = cached
                    binding = self._position_bindings[token]
                    role = self._position_roles.get(token)
                    required = True
                elif preference:
                    # 当前对话的特殊输入位置优先于其他对话的主框偏好。
                    binding = None
                    role = preference.get('input_role')
                    required = True
                elif binding and binding[0].title != target.title and not role:
                    # 新对话没有选过位置：只寻找可确认的主框，不沿用旧问卷／回答框。
                    token = self._position_cached(target, main=True)
                    binding = self._position_bindings.get(token)
                    role = self._position_roles.get(token)
                    auto_main = True
                if role:
                    role = tuple(role)
        resolver = getattr(self.backend, 'resolve_main_input', None)
        if recover and resolver and (role or auto_main):
            try:
                element = resolver(target, role)
            except TargetError as error:
                raise PositionRelocationRequired(str(error)) from error
            if not role:
                role = self.backend.input_position_role(target, element)
            with self._lock:
                if epoch != self._sync_epoch or self._stop.is_set():
                    raise TargetError('目标已切换，本次未填入。')
                token = self._position_remember_live(target, element, role, '主输入框')
                binding = (target, element)
                self._focus_token = None
        elif required and not binding:
            raise PositionRelocationRequired('上次的特殊输入框不能跨重启恢复；请重新选择输入位置。')
        if binding:
            original = binding[0]
            if original.title != target.title:
                raise PositionRelocationRequired('目标对话已变化；请重新确认输入位置。')
            with self._lock:
                if recover and (epoch != self._sync_epoch or self._stop.is_set()):
                    raise TargetError('目标已切换，本次未填入。')
                self._position_token = token
                self._position_label = self._position_labels[token]
        elif auto_main:
            raise PositionRelocationRequired('新对话的主输入框暂时无法确认，请重新选择输入位置。')
        setter = getattr(self.backend, 'set_input_binding', None)
        if setter:
            try:
                setter(binding[0] if binding else target, binding[1] if binding else None)
            except TargetError as error:
                if binding is None:
                    raise
                raise PositionRelocationRequired(str(error)) from error
        elif binding:
            raise TargetError('当前平台不支持指定输入位置。')

    def _positions(self, callback, request):
        from talkman_target import TargetError, TargetWindow
        action, generation, argument = request
        try:
            def current():
                return generation == self._position_generation and not self._stop.is_set()
            if not current():
                return
            if action == 'cancel':
                self._position_preview = None
                self._emit(callback, 'cancelled', None)
                return
            if action == 'begin':
                target = argument or self._selected
                if target is None:
                    windows = [TargetWindow.from_value(w) for w in self.backend.list_windows()]
                    target = self._resolve(windows)
                if target is None:
                    raise TargetError('请先选择一个已打开的 Agent 窗口。')
                validate = getattr(self.backend, 'validate_window', None)
                if validate:
                    target = validate(target)
                    if target is None:
                        raise TargetError('目标窗口已失效，请重新选择。')
                    target = TargetWindow.from_value(target)
                method = getattr(self.backend, 'list_input_positions', None)
                if method is None:
                    raise TargetError('当前平台暂不支持输入位置选择。')
                choices = method(target)
                if not current():
                    return
                if not choices:
                    raise TargetError('没有找到可定位的输入框，请打开需要填写的页面后刷新。')
                self._position_preview = [target, choices, 0]
            elif self._position_preview is None:
                raise TargetError('请先打开输入位置选择。')
            target, choices, index = self._position_preview
            if action in ('next', 'previous'):
                index = (index + (1 if action == 'next' else -1)) % len(choices)
            elif action in ('index', 'choose'):
                if type(argument) is not int or not 0 <= argument < len(choices):
                    raise TargetError(f'这里只有 {len(choices)} 个输入位置。')
                index = argument
            self._position_preview[2] = index
            element = choices[index]
            rect = self.backend.validate_input_position(target, element)
            if not current():
                return
            if action in ('confirm', 'choose'):
                with self._lock:
                    if not current():
                        return
                    metadata = dict(target.metadata(), input_mode='explicit')
                    role = getattr(self.backend, 'input_position_role', lambda *args: None)(target, element)
                    if role:
                        metadata['input_role'] = list(role)
                    old_metadata = self.selection.metadata
                    old_positions = self.selection.positions[:]
                    self.selection.metadata = metadata
                    self.selection.remember_position(metadata)
                    try:
                        self.selection.save()
                    except Exception:
                        self.selection.metadata = old_metadata
                        self.selection.positions = old_positions
                        raise
                    self._position_remember_live(target, element, role, f'位置 {index + 1}')
                    self._selected = target
                    self._focus_token = None
                    self._sync_epoch += 1
                    self._pending_send = None
                    self._position_active = False
                self._position_preview = None
                self._emit(callback, 'confirmed', dict(target=target, index=index, count=len(choices)))
            else:
                if action != 'inspect':
                    self.backend.preview_input_position(target, element)
                if current():
                    visible = []
                    for candidate_index, candidate in enumerate(choices):
                        try:
                            candidate_rect = self.backend.validate_input_position(target, candidate)
                        except TargetError:
                            continue
                        visible.append(dict(index=candidate_index, rect=candidate_rect))
                    self._emit(callback, 'inspect' if action == 'inspect' else 'ok', dict(index=index, count=len(choices), rect=rect,
                                                   title=target.display_name(), target=target, choices=visible))
        except TargetError as error:
            if generation == self._position_generation:
                self._emit(callback, 'error', str(error))
        except Exception:
            if generation == self._position_generation:
                self._emit(callback, 'error', '输入框已变化或暂时无法读取，请刷新后重新选择。')
