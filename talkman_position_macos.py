"""Mac 输入位置按 AX 元素绑定；不依赖输入框的聊天语义名称。"""
import math
from talkman_target import TargetError


class MacInputPositions:
    def _position_rect(self, element):
        api = self._ensure_api()
        position, size = api.get(element, 'AXPosition'), api.get(element, 'AXSize')
        if hasattr(api, 'rect_values'):
            rect = api.rect_values(position, size)
        else:
            rect = tuple(position or ()) + tuple(size or ())
        if len(rect) != 4 or not all(math.isfinite(v) for v in rect) or min(rect[2:]) < 3:
            raise TargetError('输入框没有可见的位置，请打开该区域后刷新。')
        return tuple(float(v) for v in rect)

    def validate_input_position(self, target, element):
        record = self._validate(target)
        if record is None or str(self._api.get(record.element, 'AXTitle', '') or '') != target.title:
            raise TargetError('目标窗口或对话已变化，请重新选择输入位置。')
        if not self._belongs(element, record) or not self._is_editable(element):
            raise TargetError('选定的输入框已消失或不可编辑，请重新选择输入位置。')
        return self._position_rect(element)

    def list_input_positions(self, target, *, activate=True):
        record = self._validate(target)
        if record is None:
            raise TargetError('目标窗口已失效，请重新选择。')
        api = self._api
        if activate:
            api.activate(record.app, record.element)
            self._wait_focus(target)
        pending, seen, result = [record.element], [], []
        while pending:
            element = pending.pop()
            if any(api.equal(element, other) for other in seen):
                continue
            seen.append(element)
            if len(seen) > 3000:
                raise TargetError('目标界面过大，请先关闭不需要的面板再选择。')
            if self._is_editable(element) and self._belongs(element, record):
                try:
                    self.validate_input_position(target, element)
                except TargetError:
                    pass
                else:
                    result.append(element)
                    if len(result) > 30:
                        raise TargetError('可编辑区域超过 30 个，请先关闭不需要的面板。')
            pending.extend(api.get(element, 'AXChildren', []) or [])
        return sorted(result, key=lambda e: (self._position_rect(e)[1], self._position_rect(e)[0]))

    def input_position_role(self, target, element):
        from talkman_input_positions import main_input_role
        return main_input_role(target, self._api.get(element, 'AXIdentifier', ''),
            [self._api.get(element, attr, '') for attr in ('AXTitle', 'AXDescription', 'AXPlaceholderValue')])

    def resolve_main_input(self, target, role=None):
        choices = [e for e in self.list_input_positions(target, activate=False)
                   if self.input_position_role(target, e) and
                   (role is None or self.input_position_role(target, e) == role)]
        if len(choices) != 1:
            raise TargetError('当前对话的主输入框无法唯一确认，请点「输入位置」重新选择。')
        self.validate_input_position(target, choices[0])
        return choices[0]

    def preview_input_position(self, target, element):
        self.validate_input_position(target, element)
        record = self._validate(target)
        self._api.activate(record.app, record.element)
        self._wait_focus(target)

    def set_input_binding(self, target, element):
        self._position_binding = None
        if element is not None:
            self.validate_input_position(target, element)
            self._position_binding = (target, element)

    def input_allows_submit(self):
        binding = getattr(self, '_position_binding', None)
        if binding is None:
            return True
        target, element = binding
        self.validate_input_position(target, element)
        return str(self._api.get(element, 'AXIdentifier', '')).casefold() == 'prompt-textarea'

    def _bound_input(self, target):
        binding = getattr(self, '_position_binding', None)
        if binding is None:
            return None
        original, element = binding
        if (original.session_key() != target.session_key() or original.identity != target.identity
                or original.process_started != target.process_started or original.title != target.title):
            raise TargetError('目标窗口或对话已变化，请重新选择输入位置。')
        self.validate_input_position(original, element)
        return element
