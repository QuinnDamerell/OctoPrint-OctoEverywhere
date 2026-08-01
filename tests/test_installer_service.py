import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from py_installer.Context import OsTypes
from py_installer.Service import Service


class TestSonicPadService(unittest.TestCase):

    def test_procd_environment_is_set_in_one_parameter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service_file_path = os.path.join(temp_dir, "octoeverywhere_service")
            context = SimpleNamespace(
                OsType=OsTypes.SonicPad,
                RepoRootFolder="/usr/share/octoeverywhere",
                VirtualEnvPath="/usr/share/octoeverywhere-env",
                ServiceFilePath=service_file_path,
                SkipSudoActions=False,
            )

            with patch("py_installer.Service.Util.RunShellCommand", return_value=(0, "", "")):
                Service()._InstallSonicPadAndK2(context, "encoded-args", "moonraker_octoeverywhere")

            with open(service_file_path, "r", encoding="utf-8") as service_file:
                contents = service_file.read()

            env_lines = [line.strip() for line in contents.splitlines() if "procd_set_param env" in line]
            self.assertEqual(1, len(env_lines))
            self.assertIn("HOME=/root", env_lines[0])
            self.assertIn("PYTHONPATH=/usr/share/octoeverywhere", env_lines[0])
            self.assertIn("MALLOC_TRIM_THRESHOLD_=65536", env_lines[0])
            self.assertIn("MALLOC_ARENA_MAX=2", env_lines[0])


if __name__ == "__main__":
    unittest.main()
