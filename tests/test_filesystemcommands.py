import unittest

from tests.test_dependency_stubs import InstallTestDependencyStubs

InstallTestDependencyStubs()

# pylint: disable=wrong-import-position
from octoeverywhere.filesystemcommands import FileSystemCommandHelper, VirtualFilePath  # noqa: E402


class TestUploadStoredPath(unittest.TestCase):
    # Moonraker strips non-ascii characters from uploaded file names, so the stored name can differ from
    # the requested one. The upload result has to report the stored name or follow up calls hit a 404.
    def test_renamed_upload_reports_the_stored_path(self) -> None:
        requested = VirtualFilePath("gcode", "oe_mcp_ünïcodé.gcode")
        body = b'{"action":"create_file","item":{"path":"oe_mcp__n_cod_.gcode","root":"gcodes","size":24}}'

        response = FileSystemCommandHelper.BuildFileUploadSuccess(requested, "oe_mcp_ünïcodé.gcode", 24, body)

        self.assertEqual(response.ResultDict["VirtualPath"], "gcode/oe_mcp__n_cod_.gcode")
        self.assertEqual(response.ResultDict["PlatformPath"], "oe_mcp__n_cod_.gcode")

    def test_unchanged_upload_keeps_the_requested_path(self) -> None:
        requested = VirtualFilePath("gcode", "models/benchy.gcode")
        body = b'{"action":"create_file","item":{"path":"models/benchy.gcode","root":"gcodes","size":10}}'

        response = FileSystemCommandHelper.BuildFileUploadSuccess(requested, "models/benchy.gcode", 10, body)

        self.assertEqual(response.ResultDict["VirtualPath"], "gcode/models/benchy.gcode")
        self.assertEqual(response.ResultDict["PlatformPath"], "models/benchy.gcode")

    # Platforms that don't report a stored path must keep working and fall back to the requested path.
    def test_missing_printer_path_falls_back_to_the_requested_path(self) -> None:
        requested = VirtualFilePath("gcode", "benchy.gcode")

        for body in (b'{"done":true}', b'not json', b''):
            response = FileSystemCommandHelper.BuildFileUploadSuccess(requested, "benchy.gcode", 5, body)
            self.assertEqual(response.ResultDict["VirtualPath"], "gcode/benchy.gcode")


class TestFileDetails(unittest.TestCase):
    c_Path = VirtualFilePath("gcode", "models/benchy.gcode")

    # A platform only fills in what it knows, so unknown fields must be absent rather than reported as 0.
    # A caller has to be able to tell "the platform doesn't know" from "the value really is zero".
    def test_unknown_fields_are_omitted(self) -> None:
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(
            self.c_Path, "models/benchy.gcode", {"SizeBytes": 100, "EstPrintTimeSec": None})

        self.assertEqual(response.ResultDict["SizeBytes"], 100)
        self.assertNotIn("EstPrintTimeSec", response.ResultDict)
        self.assertNotIn("LayerCount", response.ResultDict)

    def test_zero_is_kept(self) -> None:
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(
            self.c_Path, "models/benchy.gcode", {"EstFilamentUsedMm": 0})
        self.assertEqual(response.ResultDict["EstFilamentUsedMm"], 0)

    def test_paths_and_name(self) -> None:
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(self.c_Path, "models/benchy.gcode", {})
        self.assertEqual(response.ResultDict["VirtualPath"], "gcode/models/benchy.gcode")
        self.assertEqual(response.ResultDict["PlatformPath"], "models/benchy.gcode")
        self.assertEqual(response.ResultDict["FileName"], "benchy.gcode")

    # Anything the platform reports that has no dedicated field must still reach the caller.
    def test_platform_details_pass_through(self) -> None:
        raw = {"some_platform_only_field": 42}
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(self.c_Path, "models/benchy.gcode", {}, raw)
        self.assertEqual(response.ResultDict["PlatformDetails"], raw)

    def test_no_platform_details_key_when_absent(self) -> None:
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(self.c_Path, "models/benchy.gcode", {})
        self.assertNotIn("PlatformDetails", response.ResultDict)

    # An unknown key must not silently appear in the response, so the shape stays predictable.
    def test_unlisted_fields_are_dropped(self) -> None:
        response = FileSystemCommandHelper.BuildFileDetailsSuccess(
            self.c_Path, "models/benchy.gcode", {"NotARealField": 5})
        self.assertNotIn("NotARealField", response.ResultDict)


class TestDetailCoercion(unittest.TestCase):
    def test_int_coercion(self) -> None:
        self.assertEqual(FileSystemCommandHelper.AsIntOrNone("42"), 42)
        self.assertEqual(FileSystemCommandHelper.AsIntOrNone(42.7), 42)
        self.assertIsNone(FileSystemCommandHelper.AsIntOrNone(None))
        self.assertIsNone(FileSystemCommandHelper.AsIntOrNone("abc"))
        # bool is a subclass of int in python, so it has to be excluded explicitly.
        self.assertIsNone(FileSystemCommandHelper.AsIntOrNone(True))

    def test_float_coercion(self) -> None:
        self.assertEqual(FileSystemCommandHelper.AsFloatOrNone("0.2"), 0.2)
        self.assertIsNone(FileSystemCommandHelper.AsFloatOrNone(None))
        self.assertIsNone(FileSystemCommandHelper.AsFloatOrNone("nope"))
        self.assertIsNone(FileSystemCommandHelper.AsFloatOrNone(False))

    def test_string_coercion(self) -> None:
        self.assertEqual(FileSystemCommandHelper.AsStringOrNone("  PLA "), "PLA")
        self.assertIsNone(FileSystemCommandHelper.AsStringOrNone(None))
        self.assertIsNone(FileSystemCommandHelper.AsStringOrNone("   "))


class TestResolveRequestedRoots(unittest.TestCase):
    c_Roots = ["gcode", "config", "logs"]

    def test_plain_root_resolves(self) -> None:
        roots, error = FileSystemCommandHelper.ResolveRequestedRoots({"root": "gcode"}, self.c_Roots)
        self.assertIsNone(error)
        self.assertEqual(roots, ["gcode"])

    # A full path still resolves to its first segment, which is how a `path` arg selects a root.
    def test_full_path_resolves_to_its_root(self) -> None:
        roots, error = FileSystemCommandHelper.ResolveRequestedRoots({"root": "gcode/models/benchy.gcode"}, self.c_Roots)
        self.assertIsNone(error)
        self.assertEqual(roots, ["gcode"])

    # A relative segment used to be silently ignored, which listed 'gcode' for a root the caller never asked for.
    def test_relative_segments_are_rejected(self) -> None:
        for value in ("gcode/../..", "gcode/.", "../gcode"):
            roots, error = FileSystemCommandHelper.ResolveRequestedRoots({"root": value}, self.c_Roots)
            self.assertIsNotNone(error, f"expected '{value}' to be rejected")
            self.assertEqual(roots, [])

    def test_unknown_root_is_rejected(self) -> None:
        roots, error = FileSystemCommandHelper.ResolveRequestedRoots({"root": "bogus"}, self.c_Roots)
        self.assertIsNotNone(error)
        self.assertEqual(roots, [])

    def test_no_root_returns_every_root(self) -> None:
        roots, error = FileSystemCommandHelper.ResolveRequestedRoots({}, self.c_Roots)
        self.assertIsNone(error)
        self.assertEqual(roots, self.c_Roots)


if __name__ == "__main__":
    unittest.main()
