"""Round-trip real Windows process arguments without running the installer."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELLS = [path for name in ("powershell", "pwsh") if (path := shutil.which(name))]


@unittest.skipUnless(sys.platform == "win32" and POWERSHELLS, "Windows PowerShell required")
class InstallerArgumentTests(unittest.TestCase):
    def test_native_arguments_round_trip_in_windows_powershell_and_pwsh(self):
        arguments = [
            "plain", "项目 - 副本 (draft) [1] & user's", "-leading-option",
            "C:\\project with spaces\\", "C:\\", 'embedded "quotes"',
            'backslash\\"quote', 'two\\\\"quote',
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            working_directory = base / "项目 - copy [1] & user's"
            working_directory.mkdir()
            request = base / "request.json"
            request.write_text(json.dumps({
                "installer": str(ROOT / "install.ps1"),
                "python": sys.executable,
                "cwd": str(working_directory),
                "arguments": ["-c", "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]]))", *arguments],
            }), encoding="utf-8")
            script = base / "probe.ps1"
            script.write_text('''param([string]$RequestPath)
$ErrorActionPreference = "Stop"
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $request.installer, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count) { throw $parseErrors[0] }
$definition = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Invoke-Native"
}, $true)
Invoke-Expression $definition.Extent.Text
$result = Invoke-Native -FilePath $request.python -Arguments $request.arguments -WorkingDirectory $request.cwd
if ($result.ExitCode -ne 0) { throw $result.Stderr }
$result.Stdout
''', encoding="utf-8-sig")
            for powershell in POWERSHELLS:
                with self.subTest(powershell=powershell):
                    result = subprocess.run(
                        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), str(request)],
                        capture_output=True, encoding="utf-8", errors="replace",
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual([str(working_directory), arguments], json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
