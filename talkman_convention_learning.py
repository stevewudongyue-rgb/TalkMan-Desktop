"""近期短词纠正候选：只存词对及证据摘要，确认前不参与替换。"""
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from talkman_conventions import convention_key

WINDOW = 7 * 86400
THRESHOLD = 3
LIMIT = 300


def short_term(value):
    return (isinstance(value, str) and 2 <= len(value) <= 24 and value == value.strip()
            and bool(re.fullmatch(r"[\w\u3400-\u9fff]+(?:[ .'-][\w\u3400-\u9fff]+){0,3}", value))
            and not any(c.isdigit() for c in value)
            and value not in {'可以', '不行', '不能', '不要', '不是', '同意', '不同意'})


def correction_pair(before, after, selection=None):
    """只提取一处短词替换。优先保留原选词边界，不把中文单字猜成词。"""
    if before == after:
        return None
    if selection:
        start, end = selection
        new_end = end + len(after) - len(before)
        if (0 <= start < end <= len(before) and start < new_end <= len(after)
                and before[:start] == after[:start] and before[end:] == after[new_end:]):
            old, new = before[start:end].strip(), after[start:new_end].strip()
            if short_term(old) and short_term(new) and old != new:
                return old, new
    # 限制用于比较的长度及变化数量，避免长文相似匹配占住 UI。
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    old_end, new_end = len(before), len(after)
    while old_end > start and new_end > start and before[old_end-1] == after[new_end-1]:
        old_end -= 1
        new_end -= 1
    # 英文单词内部改字时保留完整单词；不扩展中文相邻句子。
    ascii_letter = lambda c: c.isascii() and (c.isalpha() or c in "_'-")
    if start < old_end and start < new_end and ascii_letter(before[start]) and ascii_letter(after[start]):
        while start > 0 and ascii_letter(before[start-1]):
            start -= 1
        while old_end < len(before) and new_end < len(after) and ascii_letter(before[old_end]):
            old_end += 1
            new_end += 1
    old, new = before[start:old_end].strip(), after[start:new_end].strip()
    if short_term(old) and short_term(new) and old != new:
        changes = [op for op in SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if op[0] != 'equal']
        if len(changes) <= 2:
            return old, new
    return None


class CorrectionMemory:
    def __init__(self, directory, clock=time.time):
        self.path = Path(directory) / 'convention-learning.json'
        self.clock = clock
        self.enabled = True
        self.items = []
        self.error = ''
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError()
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or type(data.get('enabled')) is not bool:
                raise ValueError()
            self._validate(data['items'])
            self.enabled, self.items = data['enabled'], data['items']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.error = '近期纠正记录无法读取，已暂停学习；原文件保留。'

    @staticmethod
    def _validate(items):
        if not isinstance(items, list) or len(items) > LIMIT:
            raise ValueError('近期纠正记录格式不正确')
        keys = set()
        for item in items:
            key = (item['old'], item['new'])
            if key in keys or not all(short_term(v) for v in key) or key[0] == key[1]:
                raise ValueError('近期纠正词对不正确')
            keys.add(key)
            if type(item['ignored']) is not bool or not isinstance(item['events'], list) or len(item['events']) > 20:
                raise ValueError()
            for event in item['events']:
                if not re.fullmatch('[0-9a-f]{64}', event['id']) or not math.isfinite(event['at']):
                    raise ValueError()
            if not math.isfinite(item['snooze']):
                raise ValueError()

    def _save(self, items, enabled=None):
        if self.error:
            raise ValueError(self.error)
        enabled = self.enabled if enabled is None else enabled
        self._validate(items)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='convention-learning-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(dict(version=1, enabled=enabled, items=items), stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        self.items, self.enabled = items, enabled

    def _recent(self):
        now = self.clock()
        result = deepcopy(self.items)
        for item in result:
            item['events'] = [e for e in item['events'] if now-WINDOW <= e['at'] <= now]
        return [item for item in result if item['events'] or item['ignored'] or item['snooze'] > now]

    def observe(self, old, new, evidence):
        if not self.enabled or self.error or not all(short_term(v) for v in (old, new)) or old == new:
            return False
        items = self._recent()
        item = next((x for x in items if (x['old'], x['new']) == (old, new)), None)
        if item is None:
            item = dict(old=old, new=new, events=[], ignored=False, snooze=0)
            items.append(item)
        key = hashlib.sha256(evidence.encode('utf-8')).hexdigest()
        if item['ignored'] or any(e['id'] == key for e in item['events']):
            return False
        item['events'] = [*item['events'], dict(id=key, at=self.clock())][-20:]
        # 最近更新的词保留在末尾；达到上限后淘汰最旧候选。
        items.remove(item)
        items.append(item)
        self._save(items[-LIMIT:])
        return True

    def candidates(self, conventions=(), word=None, ready=False):
        if not self.enabled or self.error:
            return []
        items = self._recent()
        owners = {convention_key(v): convention_key(item['word']) for item in conventions if item['enabled']
                  for v in [item['word'], *item['aliases']]}
        choices = []
        for item in items:
            old, new = item['old'], item['new']
            if item['ignored'] or (word is not None and convention_key(new) != convention_key(word)):
                continue
            count = len(item['events'])
            if not count or (ready and item['snooze'] > self.clock()):
                continue
            # 已有明确归属或近期存在相反／不同纠正方向时，不猜哪个词才正确。
            if convention_key(old) in owners:
                continue
            if convention_key(new) in owners and owners[convention_key(new)] != convention_key(new):
                continue
            if any(x['events'] and ((convention_key(x['old']) == convention_key(old) and convention_key(x['new']) != convention_key(new))
                                   or (convention_key(x['old']) == convention_key(new) and convention_key(x['new']) == convention_key(old))) for x in items):
                continue
            choices.append(dict(old=old, new=new, count=count, events=item['events']))
        if ready:
            groups = {}
            for item in choices:
                key = convention_key(item['new'])
                group = groups.setdefault(key, dict(old=item['old'], new=item['new'], aliases=[], evidence=set()))
                group['aliases'].append(item['old'])
                group['evidence'].update(e['id'] for e in item['events'])
            choices = [dict(old=g['old'], new=g['new'], aliases=g['aliases'], count=len(g['evidence']))
                       for g in groups.values() if len(g['evidence']) >= THRESHOLD]
        else:
            choices = [dict(old=x['old'], new=x['new'], count=x['count']) for x in choices]
        return sorted(choices, key=lambda x: -x['count'])

    def dismiss(self, old, new, forever=False):
        items = self._recent()
        for item in items:
            if convention_key(item['new']) == convention_key(new) and (old is None or item['old'] == old):
                item['ignored'] = forever
                item['snooze'] = self.clock() + 86400
        self._save(items)

    def set_enabled(self, enabled):
        self._save(self._recent(), bool(enabled))

    def clear(self):
        self._save([])
