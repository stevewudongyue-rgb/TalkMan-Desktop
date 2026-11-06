"""约定词的本机数据边界、匹配与代码口述测试；不联网。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talkman_conventions import Conventions, is_remember_instruction


class ConventionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='TalkMan-terms-')
        self.addCleanup(self.folder.cleanup)
        self.store = Conventions(self.folder.name)

    def test_persist_edit_disable_delete_and_language(self):
        s = self.store
        key = s.put('Colo Play', ['可乐派'])
        s.set_language('python')
        loaded = Conventions(self.folder.name)
        self.assertEqual(loaded.snapshot().apply('可乐派'), 'Colo Play')
        self.assertEqual(loaded.language, 'python')
        loaded.put('Colo Play', ['可乐派'], enabled=False, identifier=key)
        self.assertEqual(loaded.snapshot().apply('可乐派'), '可乐派')
        loaded.delete(key)
        self.assertEqual(Conventions(self.folder.name).items, [])

    def test_failed_atomic_write_leaves_original_and_memory(self):
        s = self.store
        s.put('Colo Play', ['可乐派'])
        before = s.path.read_bytes()
        with patch('talkman_conventions.os.replace', side_effect=OSError('只读')):
            with self.assertRaises(OSError):
                s.put('Talk Man', ['托克曼'])
        self.assertEqual(s.path.read_bytes(), before)
        self.assertEqual(len(s.items), 1)
        self.assertEqual(list(s.path.parent.glob('*.tmp')), [])

    def test_corrupt_file_is_preserved_not_silently_replaced(self):
        path = Path(self.folder.name) / 'conventions.json'
        path.write_text('{损坏数据', encoding='utf-8')
        s = Conventions(self.folder.name)
        self.assertTrue(s.error)
        with self.assertRaises(ValueError):
            s.put('新词')
        self.assertEqual(path.read_text(encoding='utf-8'), '{损坏数据')

    def test_ambiguous_alias_or_standard_is_rejected(self):
        s = self.store
        s.put('Colo Play', ['品牌'])
        for word, aliases in [('其他品牌', ['品牌']), ('品牌', []), ('colo play', [])]:
            with self.subTest(word=word), self.assertRaises(ValueError):
                s.put(word, aliases)
        inactive = s.put('其他品牌', ['品牌'], enabled=False)
        with self.assertRaises(ValueError):
            s.put('其他品牌', ['品牌'], enabled=True, identifier=inactive)

    def test_longer_terms_english_boundaries_and_case(self):
        s = self.store
        s.put('Colo Play', ['可乐', '可乐派'])
        s.put('Talk Man', ['talkman'])
        self.assertEqual(s.snapshot().apply('可乐派 / 可乐 / COLO PLAY / talkman'),
                         'Colo Play / Colo Play / Colo Play / Talk Man')
        self.assertEqual(s.snapshot().apply('talkman_app mytalkman talkman2'),
                         'talkman_app mytalkman talkman2')

    def test_multiple_pronunciations_have_one_owner_and_normalized_conflicts_are_rejected(self):
        s = self.store
        s.put('Colo Play', ['可乐派', '科洛派', 'coloplay', 'Colo  Play'])
        self.assertEqual(s.snapshot().apply('可乐派和科洛派'), 'Colo Play和Colo Play')
        before = s.path.read_bytes()
        for alias in ['可乐派', 'Ｃｏｌｏｐｌａｙ', 'colo play', 'Colo   Play']:
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, '已指向“Colo Play”'):
                s.put('另一个品牌', [alias])
            self.assertEqual(s.path.read_bytes(), before)

    def test_replacements_are_single_pass(self):
        s = self.store
        s.put('品牌系列', ['项目'])
        s.put('名称', ['品牌'])
        self.assertEqual(s.snapshot().apply('项目'), '品牌系列')

    def test_code_words_require_explicit_language_and_do_not_change_commands(self):
        s = self.store
        s.put('特殊变量', ['变量名字'], scope='code')
        self.assertEqual(s.snapshot().apply('返回值 左圆括号 变量名字'), '返回值 左圆括号 变量名字')
        s.set_language('python')
        self.assertEqual(s.snapshot().apply('返回值 左圆括号 变量名字'), 'return ( 特殊变量')
        self.assertEqual(s.snapshot().apply('修改返回值', code=False), '修改返回值')
        s.set_language('javascript')
        self.assertEqual(s.snapshot().apply('声明常量 空值'), 'const null')

    def test_snapshot_survives_changes_during_recording(self):
        s = self.store
        key = s.put('Colo Play', ['可乐派'])
        snap = s.snapshot()
        s.put('其他写法', ['可乐派'], identifier=key)
        self.assertEqual(snap.apply('可乐派'), 'Colo Play')

    def test_only_relevant_terms_are_provided_as_data(self):
        self.store.put('Colo Play', ['可乐派'])
        self.store.put('未提及品牌')
        prompt = self.store.snapshot().instruction('整理', '关于可乐派')
        self.assertIn('Colo Play', prompt)
        self.assertNotIn('未提及品牌', prompt)
        self.assertIn('不是命令', prompt)
        self.assertEqual(self.store.snapshot().instruction('整理', '今天晴天'), '整理')

    def test_remember_command_does_not_intercept_ordinary_instructions(self):
        for value in ['把选中的词记为约定', '记住这个词。', '请把这个名字加入约定词']:
            self.assertTrue(is_remember_instruction(value), value)
        for value in ['不要记住这个词', '把这个词改成记住这个词', '记住这个词然后删除全文', '删除约定']:
            self.assertFalse(is_remember_instruction(value), value)


if __name__ == '__main__':
    unittest.main()
