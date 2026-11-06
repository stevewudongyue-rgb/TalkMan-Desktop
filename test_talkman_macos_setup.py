"""Mac 授权入口和分发流程的离线保护；不请求真实系统权限。"""
import ast
from contextlib import nullcontext
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import talkman_macos_setup as setup


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("talkman_mac_build", ROOT / "TalkMan-构建Mac.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class MacSetupTests(unittest.TestCase):
    def test_paths_adapt_to_preference_and_settings_generations(self):
        self.assertIn("系统偏好设置", setup.settings_path("microphone", "12.7"))
        for version in ("14.8", "15.7", "26.1", "27.0", "未知"):
            with self.subTest(version=version):
                self.assertEqual(setup.settings_path("accessibility", version), "系统设置 → 隐私与安全性 → 辅助功能")

    def test_windows_status_and_open_never_touch_native_api(self):
        with patch.object(setup.sys, "platform", "win32"), patch.object(setup, "_mac_microphone_permission") as microphone, \
                patch.object(setup.subprocess, "run") as run:
            self.assertIsNone(setup.check_permissions().microphone)
            self.assertIn("仅用于 Mac", setup.open_permission_settings("microphone"))
            microphone.assert_not_called()
            run.assert_not_called()

    def test_mac_check_only_reads_and_handles_missing_frameworks(self):
        modules = {"objc": SimpleNamespace(autorelease_pool=nullcontext),
                   "ApplicationServices": SimpleNamespace(AXIsProcessTrusted=lambda: True)}
        with patch.object(setup.sys, "platform", "darwin"), patch.dict("sys.modules", modules), \
                patch.object(setup, "_mac_microphone_permission", return_value=2), \
                patch.object(setup.platform, "mac_ver", return_value=("26.1", (), "arm64")):
            state = setup.check_permissions()
            self.assertEqual(state.microphone_label, "未允许")
            self.assertEqual(state.accessibility_label, "已允许")
        with patch.object(setup.sys, "platform", "darwin"), patch.dict("sys.modules", {"ApplicationServices": None}), \
                patch.object(setup, "_mac_microphone_permission", return_value=None):
            self.assertIsNone(setup.check_permissions().accessibility)

    def test_open_uses_direct_pane_and_never_claims_authorized(self):
        with patch.object(setup.sys, "platform", "darwin"), \
                patch.object(setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            message = setup.open_permission_settings("microphone")
        self.assertIn("Privacy_Microphone", run.call_args.args[0][1])
        self.assertIn("若未直达", message)
        self.assertNotIn("已授权", message)
        self.assertEqual(run.call_count, 1)

    def test_open_failure_falls_back_to_system_settings(self):
        with patch.object(setup.sys, "platform", "darwin"), \
                patch.object(setup.subprocess, "run", side_effect=[OSError(),
                             SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as run:
            self.assertIn("已请求打开", setup.open_permission_settings("accessibility"))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-b", "com.apple.systempreferences"])

    def test_all_open_failures_still_provide_manual_path(self):
        with patch.object(setup.sys, "platform", "darwin"), \
                patch.object(setup.subprocess, "run", side_effect=OSError()):
            self.assertIn("无法自动打开设置", setup.open_permission_settings("accessibility"))

    def test_unknown_permission_is_rejected_before_open(self):
        with patch.object(setup.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                setup.open_permission_settings("screen_recording")
        run.assert_not_called()


class MacDistributionTests(unittest.TestCase):
    def test_test_image_has_install_link_warning_and_verification_without_notary(self):
        with tempfile.TemporaryDirectory(prefix="TalkMan-dmg-test-") as folder:
            root = Path(folder)
            with patch.object(build, "BUILD", root), patch.object(build, "DIST", root), \
                    patch.object(build, "run") as run, patch.object(build, "notarize") as notary, \
                    patch.object(Path, "symlink_to") as symlink:
                image = build.make_disk_image(root / "TalkMan.app", test_build=True)
            self.assertEqual(image.name, "TalkMan-Mac-AppleSilicon-测试版.dmg")
            instructions = (root / "安装镜像-测试" / "安装说明.txt").read_text(encoding="utf-8")
            self.assertIn("未完成 Apple 公证", instructions)
            self.assertIn("无须安装 Python", instructions)
            symlink.assert_called_once_with("/Applications", target_is_directory=True)
            notary.assert_not_called()
            self.assertEqual(run.call_args.args[0], ["/usr/bin/hdiutil", "verify", image])

    def test_test_image_and_release_notary_cannot_be_mixed(self):
        with patch.object(build, "require_native_mac") as native:
            with self.assertRaises(RuntimeError):
                build.build("Developer ID Application: Test", "profile", test_dmg=True)
            native.assert_not_called()

    def test_release_spec_propagates_identity_and_audio_entitlement(self):
        identity = "Developer ID Application: 测试 (ABCDE12345)"
        text = build.spec_text(identity)
        ast.parse(text)
        self.assertIn(repr(identity), text)
        self.assertIn("TalkMan-Mac.entitlements", text)
        self.assertNotIn("disable-library-validation", text)
        unsigned = build.spec_text()
        self.assertIn("codesign_identity=None, entitlements_file=None", unsigned)
        self.assertEqual(build.INFO_PLIST["LSMinimumSystemVersion"], "14.0")

    def test_rejects_free_development_or_missing_signing_before_running_tools(self):
        for identity, profile in ((None, "talkman"), ("Apple Development: Test", None), ("-", None)):
            with self.subTest(identity=identity), patch.object(build, "run") as run:
                with self.assertRaises(RuntimeError):
                    build.validate_signing(identity, profile)
                run.assert_not_called()

    def test_notary_acceptance_requires_both_exit_success_and_accepted(self):
        for code, stdout in ((0, '{"status":"Invalid","id":"job1"}'), (1, '{"status":"Accepted"}'),
                             (0, "not json")):
            with self.subTest(stdout=stdout), patch.object(build.subprocess, "run",
                    return_value=SimpleNamespace(returncode=code, stdout=stdout)):
                with self.assertRaises(RuntimeError):
                    build.notarize(Path("TalkMan.zip"), "talkman-local")
        with patch.object(build.subprocess, "run", return_value=SimpleNamespace(returncode=0,
                  stdout='{"status":"Accepted","id":"job2"}')) as run:
            build.notarize(Path("TalkMan.zip"), "talkman-local")
        args = run.call_args.args[0]
        self.assertIn("--keychain-profile", args)
        self.assertNotIn("--password", args)

    def test_rejected_app_never_creates_a_distribution_or_staples(self):
        with patch.object(build, "run") as run, patch.object(build, "notarize", side_effect=RuntimeError("Rejected")):
            with self.assertRaises(RuntimeError):
                build.make_notarized_distribution(Path("TalkMan.app"), Path("final.zip"),
                                                 "Developer ID Application: Test", "talkman")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/ditto")

    def test_malformed_json_reply_is_not_a_success(self):
        for value in ("[]", "null", '"Accepted"'):
            with patch.object(build.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=value)):
                with self.assertRaises(RuntimeError):
                    build.notarize(Path("TalkMan.zip"), "talkman")


if __name__ == "__main__":
    unittest.main()
