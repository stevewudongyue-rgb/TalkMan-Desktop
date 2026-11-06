"""离线验证投递凭据、完整存储与排版约束，不访问真实窗口。"""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from talkman_records import DeliveryHistory, validate_layout_only
from talkman_target import TargetController, TargetError, FillReceipt
from test_talkman_target import FakeBackend, wait_result


class RecordTests(unittest.TestCase):
    def test_all_deliveries_survive_restart_without_deduplication_or_100_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeliveryHistory(folder)
            for _ in range(103):
                store.add("同一条😀\n  ", "豆包", "项目 A", "2026-10-24T12:00:00+08:00")
            loaded = DeliveryHistory(folder)
            self.assertEqual(len(loaded.items), 103)
            self.assertEqual(loaded.items[0]["text"], "同一条😀\n  ")
            self.assertEqual(loaded.items[0]["title"], "项目 A")
            deleted = loaded.items[1]["id"]
            loaded.delete(deleted)
            self.assertNotIn(deleted, [item["id"] for item in DeliveryHistory(folder).items])
            self.assertEqual(len(DeliveryHistory(folder).items), 102)

    def test_interrupted_line_does_not_swallow_new_record(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeliveryHistory(folder)
            store.add("旧文", "豆包", "")
            with store.path.open("ab") as stream:
                stream.write(b'{"id": "interrupted')
            store.add("新文", "豆包", "新会话")
            self.assertEqual([x["text"] for x in DeliveryHistory(folder).items], ["新文", "旧文"])

    def test_storage_failure_preserves_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeliveryHistory(folder)
            store.add("旧文", "Agent", "")
            store._append = Mock(side_effect=OSError("只读"))
            store._replace = Mock(side_effect=OSError("只读"))
            with self.assertRaises(OSError):
                store.add("新文", "Agent", "")
            with self.assertRaises(OSError):
                store.delete(store.items[0]["id"])
            self.assertEqual([x["text"] for x in store.items], ["旧文"])

    def test_receipt_captures_exact_request_and_latest_window_title(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = FakeBackend()
            backend.validate_window = lambda target: replace(target, title="最新对话")
            controller = TargetController(Path(folder), backend=backend)
            self.addCleanup(controller.close)
            controller.select(backend.windows[0])
            results = []
            controller.fill_with_receipt("待填入😀  \n", lambda *args: results.append(args))
            state, receipt = wait_result(results)
            self.assertEqual(state, "ok")
            self.assertIsInstance(receipt, FillReceipt)
            self.assertEqual(receipt.title, "最新对话")
            self.assertEqual(receipt.text, "待填入😀  \n")
            self.assertEqual(receipt.app, "Agent")
            self.assertTrue(receipt.time)
            backend.fill = Mock(side_effect=TargetError("未定位输入框"))
            controller.fill_with_receipt("失败", lambda *args: results.append(args))
            self.assertEqual(wait_result(results), ("error", "未定位输入框"))

    def test_layout_rejects_rewriting_but_accepts_newlines(self):
        before = "一、买苹果。二、买梨。三、开会。"
        self.assertEqual(validate_layout_only(before, "一、买苹果。\n二、买梨。\n三、开会。"),
                         "一、买苹果。\n二、买梨。\n三、开会。")
        for result in ("", "一、买香蕉。二、买梨。三、开会。", None):
            with self.assertRaises(ValueError):
                validate_layout_only(before, result)


if __name__ == "__main__":
    unittest.main()
