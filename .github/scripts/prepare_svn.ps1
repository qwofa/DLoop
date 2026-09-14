# Configure only the hosted runner's temporary SVN copy for Unicode arguments.
$ErrorActionPreference = 'Stop'
if (-not $env:RUNNER_TEMP -or -not $env:GITHUB_PATH) {
    throw 'This setup is for a GitHub-hosted Windows runner only.'
}
$svnSource = Split-Path (Get-Command svn.exe -ErrorAction Stop).Source
$svnTarget = Join-Path $env:RUNNER_TEMP 'svn-utf8'
New-Item -ItemType Directory -Path $svnTarget -Force | Out-Null
Copy-Item -Path (Join-Path $svnSource '*') -Destination $svnTarget -Recurse
$manifestTool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\mt.exe" |
    Sort-Object FullName | Select-Object -Last 1
if (-not $manifestTool) { throw 'Windows SDK manifest tool is required.' }
$manifestPath = Join-Path $env:RUNNER_TEMP 'svn-utf8.manifest'
@'
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
    </windowsSettings>
  </application>
</assembly>
'@ | Set-Content -LiteralPath $manifestPath -Encoding utf8
foreach ($name in @('svn.exe', 'svnadmin.exe')) {
    $executable = Join-Path $svnTarget $name
    & $manifestTool.FullName -nologo "-inputresource:$executable;#1" -manifest $manifestPath "-outputresource:$executable;#1"
    if ($LASTEXITCODE -ne 0) { throw "Failed to set the process code page for $name" }
}
$svnTarget | Add-Content -LiteralPath $env:GITHUB_PATH -Encoding utf8
