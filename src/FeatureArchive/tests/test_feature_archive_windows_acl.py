"""Windows 派生索引权限继承回归测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOL_DIRECTORY = Path(__file__).resolve().parents[1]
if str(TOOL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TOOL_DIRECTORY))

import archive_validation


@unittest.skipUnless(os.name == "nt", "仅 Windows 使用 ACL 继承契约")
class FeatureArchiveWindowsAclTests(unittest.TestCase):
    @staticmethod
    def _run_icacls(*arguments: str) -> None:
        result = subprocess.run(
            ["icacls", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)

    @staticmethod
    def _current_user_sid() -> str:
        command = (
            "[System.Security.Principal.WindowsIdentity]::GetCurrent()"
            ".User.Value"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    @staticmethod
    def _acl_summary(path: Path) -> dict[str, object]:
        command = """
$ErrorActionPreference = "Stop"
$acl = Get-Acl -LiteralPath $env:FEATURE_ARCHIVE_ACL_TARGET
$hasInheritedAuthenticatedUsers = @(
    $acl.Access | Where-Object {
        if (-not $_.IsInherited) {
            return $false
        }
        try {
            $sid = $_.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            )
            return $sid.IsWellKnown(
                [System.Security.Principal.WellKnownSidType]::AuthenticatedUserSid
            )
        }
        catch {
            return $false
        }
    }
).Count -gt 0
[pscustomobject]@{
    protected = $acl.AreAccessRulesProtected
    inherited_authenticated_users = $hasInheritedAuthenticatedUsers
} | ConvertTo-Json -Compress
"""
        environment = os.environ.copy()
        environment.pop("PSMODULEPATH", None)
        environment["FEATURE_ARCHIVE_ACL_TARGET"] = str(path)
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                command,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        return json.loads(result.stdout)

    def test_replaced_indexes_inherit_target_directory_acl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "outputs"
            root.mkdir()
            self._run_icacls(
                str(root),
                "/inheritance:e",
                "/grant:r",
                "*S-1-5-11:(OI)(CI)(M)",
            )

            contents = {
                archive_validation.MACHINE_INDEX_NAME: '{"version": 2}\n',
                archive_validation.GLOBAL_INDEX_NAME: "# 新索引\n",
            }
            for name in contents:
                (root / name).write_text("旧索引\n", encoding="utf-8")

            real_mkdtemp = tempfile.mkdtemp
            current_user_sid = self._current_user_sid()

            def create_protected_staging(*args, **kwargs) -> str:
                staging = Path(real_mkdtemp(*args, **kwargs))
                self._run_icacls(
                    str(staging),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{current_user_sid}:(OI)(CI)(F)",
                )
                return str(staging)

            with mock.patch.object(
                archive_validation.tempfile,
                "mkdtemp",
                side_effect=create_protected_staging,
            ):
                changed = archive_validation._replace_derived_files(
                    root,
                    contents,
                )

            self.assertEqual(set(contents), set(changed))
            for name, expected_content in contents.items():
                path = root / name
                self.assertEqual(expected_content, path.read_text(encoding="utf-8"))
                acl = self._acl_summary(path)
                self.assertFalse(acl["protected"], f"{name}: {acl}")
                self.assertTrue(
                    acl["inherited_authenticated_users"],
                    f"{name}: {acl}",
                )


if __name__ == "__main__":
    unittest.main()
