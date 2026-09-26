import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from linux_host.config import Config


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(self.tmp.name)
        self.config.SetStr(Config.LoggingSection, Config.LogLevelKey, "INFO")
        with open(self.config.OeConfigFilePath, "rb") as f:
            self.original = f.read()


    def _AssertOriginalFilePreserved(self) -> None:
        with open(self.config.OeConfigFilePath, "rb") as f:
            self.assertEqual(f.read(), self.original)
        self.assertEqual(os.listdir(self.tmp.name), [Config.ConfigFileName])


    def test_failed_write_preserves_original_config_and_cleans_temp_file(self) -> None:
        with patch("linux_host.config.os.fsync", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                self.config.SetStr(Config.LoggingSection, Config.LogLevelKey, "DEBUG")
        self._AssertOriginalFilePreserved()


    @unittest.skipIf(os.name == "nt", "Windows doesn't support unix file permissions.")
    def test_new_config_uses_umask_permissions(self) -> None:
        # Other users and processes (like docker hosts) need to be able to read a new config, just like with open().
        with tempfile.TemporaryDirectory() as configDir:
            oldUmask = os.umask(0o022)
            try:
                config = Config(configDir)
            finally:
                os.umask(oldUmask)
            self.assertEqual(stat.S_IMODE(os.stat(config.OeConfigFilePath).st_mode), 0o644)


    def test_save_as_root_keeps_config_owner(self) -> None:
        configStat = os.stat(self.config.OeConfigFilePath)
        with patch("linux_host.config.os.geteuid", return_value=0, create=True), \
             patch("linux_host.config.os.chown", create=True) as chown:
            self.config.SetStr(Config.LoggingSection, Config.LogLevelKey, "DEBUG")
        chown.assert_called_once()
        self.assertEqual(chown.call_args[0][1:], (configStat.st_uid, configStat.st_gid))


    def test_failed_replace_preserves_original_config_and_cleans_temp_file(self) -> None:
        with patch("linux_host.config.os.replace", side_effect=PermissionError("Permission denied")):
            with self.assertRaises(PermissionError):
                self.config.SetStr(Config.LoggingSection, Config.LogLevelKey, "DEBUG")
        self._AssertOriginalFilePreserved()


    def test_successful_save_preserves_mode_comments_and_settings(self) -> None:
        os.chmod(self.config.OeConfigFilePath, 0o640)
        originalMode = stat.S_IMODE(os.stat(self.config.OeConfigFilePath).st_mode)
        self.config.SetStr(Config.WebcamSection, Config.WebcamStreamUrl, "/webcam%20stream")
        self.assertEqual(stat.S_IMODE(os.stat(self.config.OeConfigFilePath).st_mode), originalMode)
        self.config.ReloadFromFile()
        self.assertEqual(self.config.GetStr(Config.WebcamSection, Config.WebcamStreamUrl, None), "/webcam%20stream")
        with open(self.config.OeConfigFilePath, "r", encoding="utf-8") as f:
            self.assertIn("# Webcam streaming URL.", f.read())


if __name__ == "__main__":
    unittest.main()
