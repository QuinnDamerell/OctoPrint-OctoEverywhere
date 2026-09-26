import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from octoeverywhere.printinfo import PrintInfoManager


class TestPrintInfo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = PrintInfoManager(logging.getLogger("TestPrintInfo"), self.tmp.name)


    def test_missing_and_empty_lookups_preserve_active_print(self) -> None:
        current = self.manager.CreateNewPrintInfo("active", "print-id")
        for cookie in (None, "", "stale"):
            with self.subTest(cookie=cookie):
                self.assertIsNone(self.manager.GetPrintInfo(cookie))
                self.assertIs(self.manager.CurrentContext, current)
                self.assertTrue(os.path.isfile(current.FilePath))
        self.manager.CurrentContext = None
        self.assertEqual(self.manager.GetPrintInfo("active").GetPrintId(), "print-id")


    def test_corrupt_lookup_does_not_delete_other_contexts(self) -> None:
        current = self.manager.CreateNewPrintInfo("active", "print-id")
        with open(os.path.join(self.manager.ContextFolderPath, "corrupt.json"), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertIsNone(self.manager.GetPrintInfo("corrupt"))
        self.assertIs(self.manager.GetPrintInfo("active"), current)
        self.assertTrue(os.path.isfile(current.FilePath))


    def test_explicit_new_print_clears_cached_context_with_same_cookie(self) -> None:
        self.manager.CreateNewPrintInfo("same-file", "old-print-id")
        self.manager.ClearAllPrintInfos()
        self.assertIsNone(self.manager.GetPrintInfo("same-file"))
        current = self.manager.CreateNewPrintInfo("same-file", "new-print-id")
        self.assertEqual(current.GetPrintId(), "new-print-id")


    def test_empty_cookie_cannot_create_context(self) -> None:
        with self.assertRaises(ValueError):
            self.manager.CreateNewPrintInfo("", "print-id")
        self.assertEqual(os.listdir(self.manager.ContextFolderPath), [])


    def test_failed_save_preserves_last_valid_file_and_cleans_temp_file(self) -> None:
        current = self.manager.CreateNewPrintInfo("active", "print-id")
        with patch("octoeverywhere.printinfo.json.dump", side_effect=OSError("Disk full")):
            self.assertFalse(current.Save())
        with open(current.FilePath, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["PrintId"], "print-id")
        self.assertEqual(os.listdir(self.manager.ContextFolderPath), ["active.json"])


if __name__ == "__main__":
    unittest.main()
