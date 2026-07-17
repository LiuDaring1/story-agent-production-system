from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from secret_store import import_secret_from_clipboard, keychain_service, read_secret, write_secret


class SecretStoreTests(unittest.TestCase):
    def test_environment_has_priority_without_subprocess(self) -> None:
        with patch.dict(os.environ, {"TOAPIS_API_KEY": "test-only-value"}, clear=False):
            with patch("secret_store.subprocess.run") as runner:
                self.assertEqual(read_secret("TOAPIS_API_KEY"), "test-only-value")
                runner.assert_not_called()

    def test_keychain_service_name_is_scoped(self) -> None:
        self.assertEqual(keychain_service("TOAPIS_API_KEY"), "story-agent.TOAPIS_API_KEY")

    def test_write_secret_passes_value_directly_to_keychain_without_printing(self) -> None:
        with patch("secret_store.sys.platform", "darwin"), patch.dict(os.environ, {"USER": "tester"}, clear=False):
            with patch(
                "secret_store.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ) as runner:
                write_secret("TOAPIS_API_KEY", "test-secret-value")
        command = runner.call_args.args[0]
        self.assertEqual(command[:7], ["security", "add-generic-password", "-a", "tester", "-s", "story-agent.TOAPIS_API_KEY", "-U"])
        self.assertEqual(command[-2:], ["-w", "test-secret-value"])

    def test_write_secret_rejects_empty_or_unscoped_name(self) -> None:
        with self.assertRaises(ValueError):
            write_secret("bad-name", "value")
        with self.assertRaises(ValueError):
            write_secret("TOAPIS_API_KEY", "   ")

    def test_clipboard_import_stores_then_clears_without_printing_secret(self) -> None:
        calls = [
            subprocess.CompletedProcess([], 0, stdout="clipboard-secret", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        with patch("secret_store.sys.platform", "darwin"), patch.dict(os.environ, {"USER": "tester"}, clear=False):
            with patch("secret_store.subprocess.run", side_effect=calls) as runner:
                import_secret_from_clipboard("TOAPIS_API_KEY")
        self.assertEqual(runner.call_args_list[0].args[0], ["pbpaste"])
        self.assertEqual(runner.call_args_list[1].args[0][-2:], ["-w", "clipboard-secret"])
        self.assertEqual(runner.call_args_list[2].args[0], ["pbcopy"])
        self.assertEqual(runner.call_args_list[2].kwargs["input"], "")


if __name__ == "__main__":
    unittest.main()
