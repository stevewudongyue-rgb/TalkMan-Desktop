"""Windows 多输入框：以 UIA 控件身份绑定，不按名称或坐标猜测填入位置。"""
import math
import time
from talkman_target import TargetError
from talkman_windows_coordinates import physical_screen_coordinates


class WindowsInputPositions:
    def _position_rect(self, element):
        with physical_screen_coordinates():
            r = self._current(element, 'CurrentBoundingRectangle')
            try:
                rect = tuple(float(v) for v in (r.left, r.top, r.right - r.left, r.bottom - r.top))
            except AttributeError:
                rect = tuple(float(v) for v in r) if r is not None else ()
        if len(rect) != 4 or not all(math.isfinite(v) for v in rect) or min(rect[2:]) < 3:
            raise TargetError('输入框没有可见的位置，请打开该区域后刷新。')
        return rect

    def validate_input_position(self, target, element):
        valid = self.validate_window(target)
        if valid is None or valid.title != target.title:
            raise TargetError('目标窗口或对话已变化，请重新选择输入位置。')
        if not self._is_editable(element) or not self._is_descendant_of(element, self._window_element(valid)):
            raise TargetError('选定的输入框已消失或不可编辑，请重新选择输入位置。')
        hwnd = self._current(element, 'CurrentNativeWindowHandle', 0)
        if hwnd:
            from talkman_target_windows import _win_api
            user = _win_api()
            if not user.IsWindow(int(hwnd)) or not user.IsWindowVisible(int(hwnd)):
                raise TargetError('选定的输入框已隐藏或关闭，请重新选择输入位置。')
        return self._position_rect(element)

    def list_input_positions(self, target, *, activate=True):
        if activate:
            self._activate_position_window(target)
        root = self._window_element(target)
        result = []
        for kind in (self._CONTROL_EDIT, self._CONTROL_DOCUMENT, self._CONTROL_GROUP):
            condition = self._uia.CreatePropertyCondition(self._PROPERTY_CONTROL_TYPE, kind)
            items = root.FindAll(self._TREE_DESCENDANTS, condition)
            if int(items.Length) > 3000:
                raise TargetError('可编辑区域过多，请先关闭不需要的面板再选择。')
            for element in self._collection_items(items):
                if not self._is_editable(element) or any(self._uia.CompareElements(element, e) for e in result):
                    continue
                try:
                    self.validate_input_position(target, element)
                except TargetError:
                    continue
                result.append(element)
                if len(result) > 30:
                    raise TargetError('可编辑区域超过 30 个，请先关闭不需要的面板。')
        return sorted(result, key=lambda e: (self._position_rect(e)[1], self._position_rect(e)[0]))

    def input_position_role(self, target, element):
        from talkman_input_positions import main_input_role
        return main_input_role(target, self._current(element, 'CurrentAutomationId', ''),
                               [self._current(element, 'CurrentName', '')],
                               native_class=self._current(element, 'CurrentClassName', ''))

    def resolve_main_input(self, target, role=None):
        # 后台／最小化的 Electron 窗口可能暂时不暴露 UIA；先恢复窗口再有限重试。
        # 这里只定位，不写入，不重试粘贴；多个候选始终交还用户决定。
        choices = []
        for attempt in range(4):
            choices = [e for e in self.list_input_positions(target, activate=False)
                       if self.input_position_role(target, e) and
                       (role is None or self.input_position_role(target, e) == role)]
            if choices:
                break
            if attempt == 0:
                self._activate_position_window(target)
            if attempt < 3:
                time.sleep(.08)
        if len(choices) != 1:
            raise TargetError('当前对话的主输入框无法唯一确认，请点「输入位置」重新选择。')
        self.validate_input_position(target, choices[0])
        return choices[0]

    def _activate_position_window(self, target):
        from talkman_target_windows import _win_api
        if self.validate_window(target) is None:
            raise TargetError('目标窗口已失效，请重新选择。')
        user = _win_api()
        if user.IsIconic(int(target.id)):
            user.ShowWindow(int(target.id), 9)
        user.SetForegroundWindow(int(target.id))
        deadline = time.monotonic() + .5
        while int(user.GetForegroundWindow() or 0) != int(target.id):
            if time.monotonic() >= deadline:
                raise TargetError('系统未允许展示目标窗口，请先打开目标后重试。')
            time.sleep(.02)

    def preview_input_position(self, target, element):
        self.validate_input_position(target, element)
        self._activate_position_window(target)

    def set_input_binding(self, target, element):
        self._position_binding = None
        if element is not None:
            self.validate_input_position(target, element)
            self._position_binding = (target, element)

    def input_allows_submit(self):
        # 问卷、代码区等输入框的 Enter 不是聊天发送。手选位置只在已知聊天
        # composer 标识时沿用回车下发，其余不冒充“提交回答”。
        binding = getattr(self, '_position_binding', None)
        if binding is None:
            return True
        target, element = binding
        self.validate_input_position(target, element)
        role = self.input_position_role(target, element)
        if role == ('native', 'chatgpt-prosemirror'):
            # 原生客户端未提供发送语义 ID；只在唯一的编辑器仍是所选框时允许二次下发。
            choices = [e for e in self.list_input_positions(target, activate=False)
                       if self.input_position_role(target, e) == role]
            return len(choices) == 1 and bool(self._uia.CompareElements(choices[0], element))
        return role == ('id', 'prompt-textarea')

    def _bound_input(self, target):
        binding = getattr(self, '_position_binding', None)
        if binding is None:
            return None
        original, element = binding
        if (original.id != target.id or original.pid != target.pid
                or original.identity.casefold() != target.identity.casefold()
                or original.title != target.title):
            raise TargetError('目标窗口或对话已变化，请重新选择输入位置。')
        self.validate_input_position(original, element)
        return element
