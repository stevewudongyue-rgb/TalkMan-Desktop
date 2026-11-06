"""测试用的公开版功能桩，避免回归测试访问真实服务。"""
class TestEntitlements:
    __test__ = False
    message = ''
    def __init__(self, enabled=True):
        self.enabled = enabled
    def has(self, feature):
        return True
    def info(self):
        return '公开开源版 · 完整功能已开放'
    def due(self):
        return False

def licensed_app(*args, **kwargs):
    from talkman_app import TalkManApp
    kwargs.setdefault('license_client', TestEntitlements())
    return TalkManApp(*args, **kwargs)
