"""输入位置读取与高亮共用物理像素；只临时改变调用线程的 DPI 上下文。"""
import ctypes
import sys
from contextlib import contextmanager

from talkman_target import TargetError


@contextmanager
def physical_screen_coordinates():
    # UIA 的矩形也可能受调用线程 DPI 虚拟化影响，不能只在画框时切换。
    # 不按缩放倍率手算坐标，不更改进程或主窗口的缩放设置。
    if sys.platform != 'win32':
        yield
        return
    user = ctypes.WinDLL('user32', use_last_error=True)
    change = user.SetThreadDpiAwarenessContext
    change.argtypes = [ctypes.c_void_p]
    change.restype = ctypes.c_void_p
    previous = change(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    if not previous:
        raise TargetError('无法核对输入位置的屏幕缩放，请重新选择输入位置。')
    try:
        yield
    finally:
        change(previous)
