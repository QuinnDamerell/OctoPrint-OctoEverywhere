import logging
import unittest
from unittest.mock import Mock, patch

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

from octoeverywhere.octosessionimpl import OctoSession  # noqa: E402
from octoeverywhere.sentry import Sentry  # noqa: E402


class TestServerAuthFailure(unittest.TestCase):
    def test_missing_challenge_closes_session_without_duplicate_report(self) -> None:
        session = OctoSession.__new__(OctoSession)
        session.Logger = logging.getLogger("TestServerAuthFailure")
        session.ServerAuth = Mock()
        session.ServerAuth.GetEncryptedChallenge.return_value = None
        session.OnSessionError = Mock()
        session.OctoStream = Mock()
        with patch.object(Sentry, "OnException") as report:
            session.StartHandshake(0)
        session.OnSessionError.assert_called_once_with(0)
        session.OctoStream.SendMsg.assert_not_called()
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
