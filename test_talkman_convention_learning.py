"""纠正学习只提建议：阈值、隐私、持久化与冲突回归。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from talkman_convention_learning import CorrectionMemory, correction_pair, WINDOW
from talkman_conventions import Conventions


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.now = 1800000000
        self.store = CorrectionMemory(self.folder.name, clock=lambda: self.now)
        self.words = Conventions(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def record(self, old='可乐派', new='Colo Play', times=3):
        for i in range(times):
            self.store.observe(old, new, '测试输入'+str(i))

    def test_threshold_dedup_and_confirmed_alias_filter(self):
        self.record(times=2)
        self.assertFalse(self.store.candidates(ready=True))
        self.store.observe('可乐派', 'Colo Play', '测试输入1')
        self.assertEqual(self.store.candidates()[0]['count'], 2)
        self.store.observe('可乐派', 'Colo Play', '独立输入')
        self.assertEqual(self.store.candidates(ready=True)[0]['count'], 3)
        self.assertEqual(self.words.snapshot().apply('可乐派'), '可乐派')
        self.words.put('Colo Play', ['可乐派'])
        self.assertFalse(self.store.candidates(self.words.items))

    def test_expiry_restart_and_no_full_document_written(self):
        self.record()
        raw = self.store.path.read_text(encoding='utf-8')
        self.assertNotIn('测试输入', raw)
        loaded = CorrectionMemory(self.folder.name, clock=lambda: self.now)
        self.assertEqual(loaded.candidates(ready=True)[0]['count'], 3)
        self.now += WINDOW+1
        self.assertEqual(loaded.candidates(), [])

    def test_snooze_ignore_disable_and_clear(self):
        self.record()
        self.store.dismiss('可乐派', 'Colo Play')
        self.assertFalse(self.store.candidates(ready=True))
        self.now += 86401
        self.assertTrue(self.store.candidates(ready=True))
        self.store.dismiss('可乐派', 'Colo Play', True)
        self.assertFalse(self.store.candidates())
        self.store.clear()
        self.store.set_enabled(False)
        self.record()
        self.assertEqual(self.store.items, [])
        self.assertFalse(CorrectionMemory(self.folder.name).enabled)

    def test_ambiguous_and_reverse_corrections_do_not_recommend(self):
        self.record()
        self.record(new='Colo Pay', times=1)
        self.assertEqual(self.store.candidates(), [])
        self.store.clear()
        self.record()
        self.record(old='Colo Play', new='可乐派', times=1)
        self.assertEqual(self.store.candidates(), [])

    def test_existing_ownership_never_proposed_for_another_word(self):
        self.record()
        self.words.put('另一个品牌', ['可乐派'])
        self.assertFalse(self.store.candidates(self.words.items))

    def test_multiple_pronunciations_combine_independent_evidence_for_one_term(self):
        self.store.observe('可乐派', 'Colo Play', '输入1')
        self.store.observe('可乐派', 'Colo Play', '输入2')
        self.store.observe('科洛派', 'Colo Play', '输入2')
        self.assertFalse(self.store.candidates(ready=True))
        self.store.observe('科洛派', 'Colo Play', '输入3')
        item = self.store.candidates(ready=True)[0]
        self.assertEqual(item['count'], 3)
        self.assertEqual(set(item['aliases']), {'可乐派', '科洛派'})
        self.store.dismiss(None, 'Colo Play')
        self.assertFalse(self.store.candidates(ready=True))

    def test_target_that_is_another_terms_alias_is_not_recommended(self):
        self.record()
        self.words.put('另一个品牌', ['Colo Play'])
        self.assertFalse(self.store.candidates(self.words.items))

    def test_failed_write_preserves_file_and_memory(self):
        self.record(times=1)
        raw = self.store.path.read_bytes()
        with patch('talkman_convention_learning.os.replace', side_effect=OSError()):
            with self.assertRaises(OSError):
                self.store.observe('可乐派', 'Colo Play', '另一轮')
        self.assertEqual(self.store.path.read_bytes(), raw)
        self.assertEqual(self.store.candidates()[0]['count'], 1)
        self.assertFalse(list(Path(self.folder.name).glob('*.tmp')))

    def test_corrupted_store_is_preserved(self):
        for raw in ['{}', 'null', '{"version":1,"enabled":true,"items":[null]}']:
            self.store.path.write_text(raw, encoding='utf-8')
            loaded = CorrectionMemory(self.folder.name)
            self.assertTrue(loaded.error)
            self.assertFalse(loaded.observe('可乐派', 'Colo Play', 'a'))
            self.assertEqual(self.store.path.read_text(encoding='utf-8'), raw)

    def test_selection_preserves_term_and_english_word_boundaries(self):
        self.assertEqual(correction_pair('品牌：可乐拍。', '品牌：可乐派。', (3, 6)), ('可乐拍', '可乐派'))
        self.assertEqual(correction_pair('品牌：可乐派。', '品牌：Colo Play。'), ('可乐派', 'Colo Play'))
        self.assertEqual(correction_pair('use coloplay now', 'use colopaly now'), ('coloplay', 'colopaly'))

    def test_insert_delete_punctuation_single_character_and_long_prose_excluded(self):
        for before, after in [('正文', '正文新增'), ('正文', ''), ('你好', '你好。'), ('行', '不行'),
                              ('价格300', '价格400'), ('甲'*40, '乙'*40), ('品牌可乐拍', '品牌可乐派')]:
            self.assertIsNone(correction_pair(before, after), (before, after))


if __name__ == '__main__':
    unittest.main()
