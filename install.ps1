[CmdletBinding(DefaultParameterSetName = "Install")]
param(
    [Parameter(Mandatory = $true)]
    [string]$Target,

    [string]$Version,

    [Parameter(ParameterSetName = "Verify")]
    [switch]$Verify,

    [Parameter(ParameterSetName = "Uninstall")]
    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RequiredSvnIgnore = ".scratch"
$ManagedToolRelativeDirectory = "Tools\FeatureArchive"

function Get-NormalizedVersion {
    param([Parameter(Mandatory = $true)][string]$Value)

    $normalized = $Value.Trim()
    if ($normalized.StartsWith("v", [System.StringComparison]::OrdinalIgnoreCase)) {
        $normalized = $normalized.Substring(1)
    }
    return $normalized
}

function ConvertTo-SemanticVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if ($Value -notmatch "^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$") {
        throw "$Description 不是合法语义版本：$Value"
    }
    return [System.Version]::Parse($Value)
}

function Get-ReleaseVersionSupport {
    param([Parameter(Mandatory = $true)]$Release)

    $supportProperty = $Release.PSObject.Properties["versionSupport"]
    if ($null -eq $supportProperty -or $null -eq $supportProperty.Value) {
        throw "release.json 缺少版本支持矩阵 versionSupport。"
    }
    $support = $supportProperty.Value
    $modeProperty = $support.PSObject.Properties["mode"]
    if (
        $null -eq $modeProperty -or
        -not ($modeProperty.Value -is [string]) -or
        [string]$modeProperty.Value -ne "archive-on-upgrade"
    ) {
        throw "release.json 的版本支持模式必须为 archive-on-upgrade。"
    }

    $behaviorsProperty = $support.PSObject.Properties["behaviors"]
    if ($null -eq $behaviorsProperty -or $null -eq $behaviorsProperty.Value) {
        throw "release.json 的版本支持矩阵缺少 behaviors。"
    }
    $behaviors = $behaviorsProperty.Value
    $expectedBehaviors = [ordered]@{
        absent = "install-current"
        sameVersion = "verify-ownership-and-reinstall-idempotently"
        olderVersion = "archive-retire-and-install"
        newerVersion = "reject-before-read-or-write"
    }
    foreach ($name in $expectedBehaviors.Keys) {
        $property = $behaviors.PSObject.Properties[$name]
        if (
            $null -eq $property -or
            -not ($property.Value -is [string]) -or
            [string]$property.Value -ne [string]$expectedBehaviors[$name]
        ) {
            throw (
                "release.json 的版本支持矩阵行为 $name 必须为 " +
                "$($expectedBehaviors[$name])。"
            )
        }
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString(
            $sha256.ComputeHash($stream)
        )).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Write-Utf8WithoutBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Read-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($content)) {
            throw "内容为空。"
        }
        $document = $content | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $document) {
            throw "JSON 根不能是 null。"
        }
        return $document
    }
    catch {
        throw "$Description 不是合法 JSON：$Path。$($_.Exception.Message)"
    }
}

function Write-JsonDocument {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Document
    )

    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    Write-Utf8WithoutBom `
        -Path $Path `
        -Content (($Document | ConvertTo-Json -Depth 20) + "`n")
}

function Get-PluginPayloadMappings {
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $pluginRoot = Join-Path $RepositoryRoot "plugin\dloop"
    $manifestPath = Join-Path $pluginRoot "payload.json"
    $manifest = Read-JsonFile -Path $manifestPath -Description "Plugin 载荷清单"
    if (
        $null -eq $manifest -or
        $manifest.schema_version -ne 1 -or
        $null -eq $manifest.PSObject.Properties["modules"]
    ) {
        throw "Plugin 载荷清单结构不合法。"
    }
    $mappings = @()
    $identifiers = @{}
    $destinations = @{}
    foreach ($module in @($manifest.modules)) {
        $properties = @($module.PSObject.Properties.Name | Sort-Object)
        if ((Compare-Object $properties @("destination", "id", "kind", "source"))) {
            throw "Plugin 载荷模块只能声明 id、kind、source 和 destination。"
        }
        $identifier = [string]$module.id
        $kind = [string]$module.kind
        $source = [string]$module.source
        $destination = [string]$module.destination
        if (
            [string]::IsNullOrWhiteSpace($identifier) -or
            $kind -notin @("skill", "runtime", "unity-editor") -or
            [string]::IsNullOrWhiteSpace($source) -or
            [string]::IsNullOrWhiteSpace($destination) -or
            [System.IO.Path]::IsPathRooted($source) -or
            [System.IO.Path]::IsPathRooted($destination) -or
            @($source -split "[\\/]") -contains ".." -or
            @($destination -split "[\\/]") -contains ".." -or
            $identifiers.ContainsKey($identifier) -or
            $destinations.ContainsKey($destination)
        ) {
            throw "Plugin 载荷模块不合法或存在重复：$identifier"
        }
        $sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $pluginRoot $source))
        $normalizedDestination = $destination.Replace("\", "/").TrimEnd("/")
        if (
            -not (Test-PathWithin -Candidate $sourceRoot -Root $pluginRoot) -or
            -not (Test-Path -LiteralPath $sourceRoot -PathType Container) -or
            (
                $kind -eq "skill" -and
                $normalizedDestination -notmatch "^\.agents/skills/dloop(?:-[a-z0-9][a-z0-9-]*)?$"
            ) -or
            (
                $kind -eq "runtime" -and
                $normalizedDestination -ne "Tools/FeatureArchive" -and
                -not $normalizedDestination.StartsWith(
                    "Tools/FeatureArchive/",
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) -or
            (
                $kind -eq "unity-editor" -and
                $normalizedDestination -ne "Packages/com.dloop.ui-capture"
            )
        ) {
            throw "Plugin 载荷模块源目录或目标命名空间不合法：$identifier"
        }
        $identifiers[$identifier] = $true
        $destinations[$destination] = $true
        $mappings += [PSCustomObject]@{
            Kind = $kind
            SourceRoot = $sourceRoot
            DestinationRoot = $destination
        }
    }
    if ($mappings.Count -eq 0) {
        throw "Plugin 载荷清单没有模块。"
    }
    return @($mappings)
}

function Get-Payload {
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][object[]]$PluginMappings
    )

    $mappings = @($PluginMappings) + @(
        @{
            SourceRoot = Join-Path $RepositoryRoot "src\FeatureArchive"
            DestinationRoot = "Tools\FeatureArchive"
        }
    )

    $payload = @()
    foreach ($mapping in $mappings) {
        $sourceRoot = [System.IO.Path]::GetFullPath($mapping.SourceRoot)
        if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
            throw "发行载荷目录不存在：$sourceRoot"
        }
        $files = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
            Where-Object {
                $_.Extension -notin @(".pyc", ".pyo") -and
                $_.FullName -notmatch "[\\/]__pycache__[\\/]"
            }
        foreach ($file in $files) {
            $relativeSource = $file.FullName.Substring($sourceRoot.Length).TrimStart("\", "/")
            $relativeDestination = Join-Path $mapping.DestinationRoot $relativeSource
            $payload += [PSCustomObject]@{
                Source = $file.FullName
                RelativePath = $relativeDestination.Replace("\", "/")
                Hash = Get-Sha256 -Path $file.FullName
            }
        }
    }
    return @($payload | Sort-Object RelativePath)
}

function Get-LockHashMap {
    param($Lock)

    $hashes = @{}
    if ($null -eq $Lock -or $null -eq $Lock.PSObject.Properties["files"]) {
        return $hashes
    }
    foreach ($file in @($Lock.files)) {
        $path = [string]$file.path
        $hash = [string]$file.sha256
        if (
            -not $path -or
            $path.Contains("\") -or
            $path.Contains(":") -or
            $hash -notmatch "^[0-9a-fA-F]{64}$" -or
            $hashes.ContainsKey($path)
        ) {
            throw "安装锁中的受管文件记录非法或重复。"
        }
        $hashes[$path] = $hash.ToLowerInvariant()
    }
    return $hashes
}

function Find-SvnExecutable {
    $svn = Get-Command svn -ErrorAction SilentlyContinue
    if ($null -ne $svn) {
        return $svn.Source
    }
    $fallback = Join-Path $env:ProgramFiles "SlikSvn\bin\svn.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) {
        return $fallback
    }
    throw "未找到 SVN 客户端。"
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $trimCharacters = [char[]]@('\', '/')
    $normalizedCandidate = [System.IO.Path]::GetFullPath($Candidate).TrimEnd(
        $trimCharacters
    )
    $normalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        $trimCharacters
    )
    if ($normalizedCandidate.Equals(
        $normalizedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $true
    }
    $prefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    return $normalizedCandidate.StartsWith(
        $prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Resolve-ManagedRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    if (
        [string]::IsNullOrWhiteSpace($RelativePath) -or
        [System.IO.Path]::IsPathRooted($RelativePath)
    ) {
        throw "安装锁中的受管路径不是项目相对路径：$RelativePath"
    }
    $resolved = [System.IO.Path]::GetFullPath(
        (Join-Path $TargetRoot $RelativePath)
    )
    if (
        $resolved.Equals(
            [System.IO.Path]::GetFullPath($TargetRoot),
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        -not (Test-PathWithin -Candidate $resolved -Root $TargetRoot)
    ) {
        throw "安装锁中的受管路径超出目标项目：$RelativePath"
    }
    return $resolved
}

function Assert-NoReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Boundary
    )

    $current = [System.IO.Path]::GetFullPath($Path)
    $normalizedBoundary = [System.IO.Path]::GetFullPath($Boundary)
    while (Test-PathWithin -Candidate $current -Root $normalizedBoundary) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "受管路径包含链接或重解析点，拒绝处理：$Path"
            }
        }
        if ($current.Equals(
            $normalizedBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            break
        }
        $current = Split-Path -Parent $current
    }
}

function Get-ValidatedLockPayload {
    param(
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][string]$TargetRoot
    )

    $filesProperty = $Lock.PSObject.Properties["files"]
    if ($null -eq $filesProperty -or $null -eq $filesProperty.Value) {
        throw "安装锁缺少受管文件记录。"
    }
    $managedRoots = @(
        @($ManagedSkillRelativeDirectories) | ForEach-Object {
            $_.Replace("\", "/").TrimEnd("/") + "/"
        }
    ) + @(
        $ManagedToolRelativeDirectory.Replace("\", "/").TrimEnd("/") + "/"
    ) + @(
        @($ManagedUnityPackageRelativeDirectories) | ForEach-Object {
            $_.Replace("\", "/").TrimEnd("/") + "/"
        }
    )
    $seen = @{}
    $result = @()
    foreach ($file in @($filesProperty.Value)) {
        if ($null -eq $file -or -not ($file -is [System.Management.Automation.PSCustomObject])) {
            throw "安装锁中的受管文件记录不是 JSON 对象。"
        }
        $properties = @($file.PSObject.Properties)
        $names = @($properties | ForEach-Object { $_.Name } | Sort-Object)
        if ($properties.Count -ne 2 -or ($names -join ",") -ne "path,sha256") {
            throw "安装锁中的受管文件记录只能包含 path 和 sha256。"
        }
        $relativePath = [string]$file.path
        $hash = [string]$file.sha256
        if (
            [string]::IsNullOrWhiteSpace($relativePath) -or
            $relativePath.Contains("\") -or
            $relativePath.Contains(":") -or
            $hash -notmatch "^[0-9a-fA-F]{64}$" -or
            $seen.ContainsKey($relativePath)
        ) {
            throw "安装锁中的受管文件路径或哈希非法、重复：$relativePath"
        }
        $destination = Resolve-ManagedRelativePath `
            -TargetRoot $TargetRoot `
            -RelativePath $relativePath
        $canonicalRelativePath = $destination.Substring(
            [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd("\", "/").Length
        ).TrimStart("\", "/").Replace("\", "/")
        if (-not $canonicalRelativePath.Equals(
            $relativePath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "安装锁中的受管路径不是规范项目相对路径：$relativePath"
        }
        if (@($managedRoots | Where-Object {
            $relativePath.StartsWith($_, [System.StringComparison]::OrdinalIgnoreCase)
        }).Count -ne 1) {
            throw "安装锁中的路径不属于受支持版本的受管根：$relativePath"
        }
        Assert-NoReparsePoint -Path $destination -Boundary $TargetRoot
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            throw "受管文件缺失或类型变化：$relativePath"
        }
        $normalizedHash = $hash.ToLowerInvariant()
        if ((Get-Sha256 -Path $destination) -ne $normalizedHash) {
            throw "受管文件发生本地漂移：$relativePath"
        }
        $seen[$relativePath] = $true
        $result += [PSCustomObject]@{
            Destination = $destination
        }
    }
    if ($result.Count -eq 0) {
        throw "安装锁中的受管文件记录为空。"
    }
    return @($result)
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = (Get-Location).ProviderPath
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        # Start-Process joins array items with spaces; preserve each Windows argv item.
        $quotedArguments = @($Arguments | ForEach-Object {
            '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
        })
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList ($quotedArguments -join ' ') `
            -WorkingDirectory ([System.Management.Automation.WildcardPattern]::Escape($WorkingDirectory)) `
            -Wait `
            -PassThru `
            -NoNewWindow `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            Stdout = Get-Content -LiteralPath $stdoutPath -Raw -Encoding UTF8
            Stderr = Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Assert-WindowsSvnProject {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    $svn = Find-SvnExecutable
    $info = Invoke-Native `
        -FilePath $svn `
        -WorkingDirectory $TargetRoot `
        -Arguments @("info", "--xml", "--", ".")
    if ($info.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($info.Stdout)) {
        throw "SVN 工作副本检查失败（退出码 $($info.ExitCode)）。目标目录：$TargetRoot。原始错误：$($info.Stderr)"
    }
    $infoDocument = [xml]$info.Stdout
    $workingCopyRoot = [System.IO.Path]::GetFullPath(
        $infoDocument.info.entry.'wc-info'.'wcroot-abspath'
    )
    if (-not (Test-PathWithin -Candidate $TargetRoot -Root $workingCopyRoot)) {
        throw "目标目录不在 SVN 工作副本内。"
    }

    $ignore = Invoke-Native `
        -FilePath $svn `
        -WorkingDirectory $TargetRoot `
        -Arguments @("propget", "svn:ignore", "--strict", "--", ".")
    if ($ignore.ExitCode -ne 0) {
        throw "目标项目必须预先通过 svn:ignore 排除 .scratch；安装器不会修改该规则。"
    }
    $ignoreLines = @($ignore.Stdout -split "\r?\n" | Where-Object { $_ -ne "" })
    if ($ignoreLines -notcontains $RequiredSvnIgnore) {
        throw "目标项目必须预先通过 svn:ignore 排除 .scratch；安装器不会修改该规则。"
    }
}

function Assert-WindowsVcsProject {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "当前发行版只支持 Windows Git 或 SVN 项目。"
    }
    if (-not (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
        throw "目标项目不存在或不是目录：$TargetRoot"
    }
    $directory = Get-Item -LiteralPath $TargetRoot
    while ($null -ne $directory) {
        if (Test-Path -LiteralPath (Join-Path $directory.FullName ".git")) {
            $git = Get-Command git -ErrorAction SilentlyContinue
            if ($null -eq $git) { throw "未找到 Git 客户端。" }
            $info = Invoke-Native -FilePath $git.Source -Arguments @(
                "--no-optional-locks", "-C", $TargetRoot, "rev-parse", "--show-toplevel"
            )
            if ($info.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($info.Stdout)) {
                throw "目标目录不是可识别的 Git 工作副本：$($info.Stderr)"
            }
            $ignore = Invoke-Native -FilePath $git.Source -Arguments @(
                "--no-optional-locks", "-C", $TargetRoot, "check-ignore", "-q", "--", ".scratch/"
            )
            if ($ignore.ExitCode -ne 0) {
                throw "目标项目必须预先通过 Git 忽略规则排除 .scratch；安装器不会修改该规则。"
            }
            return
        }
        if (Test-Path -LiteralPath (Join-Path $directory.FullName ".svn") -PathType Container) {
            Assert-WindowsSvnProject -TargetRoot $TargetRoot
            return
        }
        $directory = $directory.Parent
    }
    throw "目标项目必须位于 Git 或 SVN 工作副本中。"
}

function New-LockDocument {
    param(
        [Parameter(Mandatory = $true)][object[]]$ManagedPayload,
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)][string]$ReleaseVersion
    )

    return [ordered]@{
        workflowVersion = $ReleaseVersion
        archiveSchemaVersion = [int]$Release.archiveSchemaVersion
        terminologySchemaVersion = [int]$Release.terminologySchemaVersion
        sourceTag = [string]$Release.sourceTag
        installedAtUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        files = @(
            $ManagedPayload | ForEach-Object {
                [ordered]@{
                    path = $_.RelativePath
                    sha256 = $_.Hash
                }
            }
        )
    }
}

function Save-FileSnapshot {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Snapshots,
        [Parameter(Mandatory = $true)][string]$Path
    )

    if ($Snapshots.ContainsKey($Path)) {
        return
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $Snapshots[$Path] = [PSCustomObject]@{
            Exists = $true
            Bytes = [System.IO.File]::ReadAllBytes($Path)
        }
    }
    else {
        $Snapshots[$Path] = [PSCustomObject]@{
            Exists = $false
            Bytes = $null
        }
    }
}

function Restore-FileSnapshots {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshots)

    foreach ($path in @($Snapshots.Keys)) {
        $snapshot = $Snapshots[$path]
        if ($snapshot.Exists) {
            $directory = Split-Path -Parent $path
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
            [System.IO.File]::WriteAllBytes($path, $snapshot.Bytes)
        }
        elseif (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Save-DirectoryHierarchySnapshot {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Snapshots,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Boundary
    )

    $normalizedBoundary = [System.IO.Path]::GetFullPath($Boundary)
    $current = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-PathWithin -Candidate $current -Root $normalizedBoundary)) {
        throw "事务目录超出目标项目范围：$current"
    }
    while (-not $current.Equals(
        $normalizedBoundary,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        if (-not $Snapshots.ContainsKey($current)) {
            if (Test-Path -LiteralPath $current) {
                if (-not (Test-Path -LiteralPath $current -PathType Container)) {
                    throw "事务目录路径不是目录：$current"
                }
                $Snapshots[$current] = $true
            }
            else {
                $Snapshots[$current] = $false
            }
        }
        $current = Split-Path -Parent $current
    }
}

function Restore-DirectorySnapshots {
    param([Parameter(Mandatory = $true)][hashtable]$Snapshots)

    $orderedPaths = @($Snapshots.Keys | Sort-Object Length)
    foreach ($path in $orderedPaths) {
        if ($Snapshots[$path] -and -not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType Directory -Path $path -Force | Out-Null
        }
    }
    foreach ($path in @($orderedPaths | Sort-Object Length -Descending)) {
        if (
            -not $Snapshots[$path] -and
            (Test-Path -LiteralPath $path -PathType Container) -and
            @(Get-ChildItem -LiteralPath $path -Force).Count -eq 0
        ) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Get-SupportedManagedState {
    param(
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][string]$TargetRoot
    )

    $payload = @(Get-ValidatedLockPayload `
        -Lock $Lock `
        -TargetRoot $TargetRoot)
    $registeredPayloadPaths = @{}
    $registeredToolDirectories = @{}
    $managedToolRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $TargetRoot $ManagedToolRelativeDirectory)
    )
    foreach ($item in $payload) {
        $destination = [System.IO.Path]::GetFullPath($item.Destination)
        $registeredPayloadPaths[$destination] = $true
        if (Test-PathWithin -Candidate $destination -Root $managedToolRoot) {
            $directory = Split-Path -Parent $destination
            while (Test-PathWithin -Candidate $directory -Root $managedToolRoot) {
                $registeredToolDirectories[$directory] = $true
                if ($directory.Equals(
                    $managedToolRoot,
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                    break
                }
                $directory = Split-Path -Parent $directory
            }
        }
    }
    $runtimeCacheFiles = @()
    $runtimeCacheDirectories = @{}
    foreach ($relativeRoot in @(
        @($ManagedSkillRelativeDirectories) +
        @($ManagedToolRelativeDirectory) +
        @($ManagedUnityPackageRelativeDirectories)
    )) {
        $managedRoot = Join-Path $TargetRoot $relativeRoot
        if (-not (Test-Path -LiteralPath $managedRoot)) {
            continue
        }
        Assert-NoReparsePoint -Path $managedRoot -Boundary $TargetRoot
        foreach ($entry in @(Get-ChildItem -LiteralPath $managedRoot -Recurse -Force)) {
            if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "受管目录包含链接或重解析点，拒绝处理：$($entry.FullName)"
            }
            $entryPath = [System.IO.Path]::GetFullPath($entry.FullName)
            if ($entry.PSIsContainer) {
                $parent = Split-Path -Parent $entryPath
                if ($entry.Name.Equals(
                    "__pycache__",
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                    if (
                        (Test-PathWithin -Candidate $entryPath -Root $managedToolRoot) -and
                        $registeredToolDirectories.ContainsKey($parent)
                    ) {
                        $runtimeCacheDirectories[$entryPath] = $true
                        continue
                    }
                    throw "受管目录包含安装锁未登记的缓存目录，拒绝处理：$entryPath"
                }
                if ((Split-Path -Leaf $parent).Equals(
                    "__pycache__",
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                    throw "受管缓存目录包含安装锁未登记的未知子目录，拒绝处理：$entryPath"
                }
                continue
            }
            if (-not $registeredPayloadPaths.ContainsKey($entryPath)) {
                $cacheDirectory = Split-Path -Parent $entryPath
                $cacheParent = Split-Path -Parent $cacheDirectory
                if (
                    [System.IO.Path]::GetExtension($entryPath).Equals(
                        ".pyc",
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -and
                    (Split-Path -Leaf $cacheDirectory).Equals(
                        "__pycache__",
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -and
                    (Test-PathWithin -Candidate $entryPath -Root $managedToolRoot) -and
                    $registeredToolDirectories.ContainsKey($cacheParent)
                ) {
                    $runtimeCacheDirectories[$cacheDirectory] = $true
                    $runtimeCacheFiles += $entryPath
                    continue
                }
                $relativePath = $entryPath.Substring(
                    [System.IO.Path]::GetFullPath($TargetRoot).TrimEnd("\", "/").Length
                ).TrimStart("\", "/").Replace("\", "/")
                throw "受管目录包含安装锁未登记的文件，拒绝处理：$relativePath"
            }
        }
    }
    return [PSCustomObject]@{
        Payload = @($payload)
        RuntimeCacheFiles = @($runtimeCacheFiles)
        RuntimeCacheDirectories = @($runtimeCacheDirectories.Keys)
    }
}

function Get-UnownedManagedResiduals {
    param([Parameter(Mandatory = $true)][string]$TargetRoot)

    $relativePaths = @($ManagedSkillRelativeDirectories) + @(
        $ManagedToolRelativeDirectory
    ) + @($ManagedUnityPackageRelativeDirectories)
    $residuals = @()
    foreach ($relativePath in $relativePaths) {
        if (Test-Path -LiteralPath (Join-Path $TargetRoot $relativePath)) {
            $residuals += $relativePath.Replace("\", "/")
        }
    }
    return @($residuals)
}

function Remove-EmptyManagedDirectories {
    param([Parameter(Mandatory = $true)][hashtable]$DirectorySnapshots)

    foreach ($directory in @($DirectorySnapshots.Keys | Sort-Object Length -Descending)) {
        if (
            (Test-Path -LiteralPath $directory -PathType Container) -and
            @(Get-ChildItem -LiteralPath $directory -Force).Count -eq 0
        ) {
            Remove-Item -LiteralPath $directory -Force
        }
    }
}

function Remove-SupportedManagedState {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)][hashtable]$Snapshots,
        [Parameter(Mandatory = $true)][hashtable]$DirectorySnapshots
    )

    Save-FileSnapshot -Snapshots $Snapshots -Path $LockPath
    Save-DirectoryHierarchySnapshot `
        -Snapshots $DirectorySnapshots `
        -Path (Split-Path -Parent $LockPath) `
        -Boundary $TargetRoot
    foreach ($item in $State.Payload) {
        Save-FileSnapshot -Snapshots $Snapshots -Path $item.Destination
        Save-DirectoryHierarchySnapshot `
            -Snapshots $DirectorySnapshots `
            -Path (Split-Path -Parent $item.Destination) `
            -Boundary $TargetRoot
    }
    foreach ($directory in $State.RuntimeCacheDirectories) {
        Save-DirectoryHierarchySnapshot `
            -Snapshots $DirectorySnapshots `
            -Path $directory `
            -Boundary $TargetRoot
    }
    foreach ($file in $State.RuntimeCacheFiles) {
        Save-FileSnapshot -Snapshots $Snapshots -Path $file
    }

    foreach ($file in $State.RuntimeCacheFiles) {
        Remove-Item -LiteralPath $file -Force
    }
    Remove-EmptyManagedDirectories -DirectorySnapshots $DirectorySnapshots
    Test-FailureInjection -Step "after-uninstall-runtime-cache"

    foreach ($item in $State.Payload) {
        Remove-Item -LiteralPath $item.Destination -Force
    }
    Remove-EmptyManagedDirectories -DirectorySnapshots $DirectorySnapshots
    Test-FailureInjection -Step "after-uninstall-payload"

    Remove-Item -LiteralPath $LockPath -Force
    Remove-EmptyManagedDirectories -DirectorySnapshots $DirectorySnapshots
}

function Test-FailureInjection {
    param([Parameter(Mandatory = $true)][string]$Step)

    if ($env:FEATURE_ARCHIVE_INSTALL_FAIL_STEP -eq $Step) {
        throw "测试故障注入：$Step"
    }
}

function Assert-InstalledState {
    param(
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][object[]]$Payload,
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][string]$ReleaseVersion
    )

    if ($null -eq $Lock) {
        throw "目标项目没有安装锁。"
    }
    if ((Get-NormalizedVersion -Value ([string]$Lock.workflowVersion)) -ne $ReleaseVersion) {
        throw "安装版本与当前发行版不一致。"
    }
    $lockHashes = Get-LockHashMap -Lock $Lock
    if ($lockHashes.Count -ne $Payload.Count) {
        throw "安装锁中的受管文件数量与发行载荷不一致。"
    }
    foreach ($item in $Payload) {
        $destination = Join-Path $TargetRoot $item.RelativePath
        if (
            -not (Test-Path -LiteralPath $destination -PathType Leaf) -or
            -not $lockHashes.ContainsKey($item.RelativePath) -or
            $lockHashes[$item.RelativePath] -ne $item.Hash -or
            (Get-Sha256 -Path $destination) -ne $item.Hash
        ) {
            throw "受管文件缺失或漂移：$($item.RelativePath)"
        }
    }
}

$repositoryRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$versionPath = Join-Path $repositoryRoot "VERSION"
$releasePath = Join-Path $repositoryRoot "release.json"
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
    throw "缺少 VERSION。"
}
if (-not (Test-Path -LiteralPath $releasePath -PathType Leaf)) {
    throw "缺少 release.json。"
}

$repositoryVersion = Get-NormalizedVersion -Value (
    Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8
)
$release = Read-JsonFile -Path $releasePath -Description "release.json"
$releaseVersion = Get-NormalizedVersion -Value ([string]$release.workflowVersion)
$null = ConvertTo-SemanticVersion `
    -Value $releaseVersion `
    -Description "当前发行版"
Get-ReleaseVersionSupport -Release $release
if ($repositoryVersion -ne $releaseVersion) {
    throw "VERSION 与 release.json 不一致：$repositoryVersion != $releaseVersion"
}
if ($Version) {
    $requestedVersion = Get-NormalizedVersion -Value $Version
    if ($requestedVersion -ne $releaseVersion) {
        throw "请求版本 $requestedVersion 与当前发行版 $releaseVersion 不一致。"
    }
}

$pluginMappings = @(Get-PluginPayloadMappings -RepositoryRoot $repositoryRoot)
$ManagedSkillRelativeDirectories = @(
    $pluginMappings |
        Where-Object { $_.Kind -eq "skill" } |
        ForEach-Object { $_.DestinationRoot }
)
$ManagedUnityPackageRelativeDirectories = @(
    $pluginMappings |
        Where-Object { $_.Kind -eq "unity-editor" } |
        ForEach-Object { $_.DestinationRoot }
)
$payload = @(Get-Payload `
    -RepositoryRoot $repositoryRoot `
    -PluginMappings $pluginMappings)
if ($payload.Count -eq 0) {
    throw "发行载荷为空。"
}

$targetRoot = [System.IO.Path]::GetFullPath($Target)
Assert-WindowsVcsProject -TargetRoot $targetRoot
$lockPath = Join-Path $targetRoot ".agents\feature-archive-workflow.lock.json"
if (Test-Path -LiteralPath $lockPath) {
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        throw "受管控制文件位置不是普通文件：$lockPath"
    }
    Assert-NoReparsePoint -Path $lockPath -Boundary $targetRoot
}
$existingLock = Read-JsonFile -Path $lockPath -Description "安装锁"
$isUpgrade = $false
$installedVersion = "uninstalled"
if ($null -eq $existingLock) {
    if ($Uninstall) {
        $residuals = @(Get-UnownedManagedResiduals `
            -TargetRoot $targetRoot)
        if ($residuals.Count -gt 0) {
            throw (
                "目标项目没有安装锁，但存在无法证明归属的 DLoop 残留，拒绝卸载：`n- " +
                ($residuals -join "`n- ")
            )
        }
        Write-Output "DLoop 未安装：未发现安装锁或受管残留。"
        exit 0
    }
    if (-not $Verify) {
        $residuals = @(Get-UnownedManagedResiduals `
            -TargetRoot $targetRoot)
        if ($residuals.Count -gt 0) {
            throw (
                "目标项目没有安装锁，但存在无法证明归属的 DLoop 残留，拒绝安装：`n- " +
                ($residuals -join "`n- ")
            )
        }
    }
}

if ($null -ne $existingLock) {
    $versionProperty = $existingLock.PSObject.Properties["workflowVersion"]
    if ($null -eq $versionProperty -or -not ($versionProperty.Value -is [string])) {
        throw "安装锁缺少合法的工作流版本。"
    }
    $installedVersion = [string]$versionProperty.Value
    $null = ConvertTo-SemanticVersion `
        -Value $installedVersion `
        -Description "安装锁中的工作流版本"
    $isUpgrade = ([System.Version]$installedVersion -lt [System.Version]$releaseVersion) -and -not ($Verify -or $Uninstall)
    if ($installedVersion -ne $releaseVersion -and -not $isUpgrade) {
        throw (
            "目标项目安装版本 $installedVersion 与当前发行版 $releaseVersion 不一致；" +
            "当前安装器不会读取其载荷结构，也不会修改目标项目。"
        )
    }
}

if ($Uninstall) {
    $installedState = Get-SupportedManagedState `
        -Lock $existingLock `
        -TargetRoot $targetRoot
    $snapshots = @{}
    $directorySnapshots = @{}
    try {
        Remove-SupportedManagedState `
            -TargetRoot $targetRoot `
            -LockPath $lockPath `
            -State $installedState `
            -Snapshots $snapshots `
            -DirectorySnapshots $directorySnapshots
    }
    catch {
        $failure = $_
        Restore-FileSnapshots -Snapshots $snapshots
        Restore-DirectorySnapshots -Snapshots $directorySnapshots
        throw $failure
    }
    Write-Output "完整卸载完成：已移除版本 $installedVersion 的受管载荷和安装锁。"
    Write-Output "交付档案、摩擦收件箱和项目版本管理忽略规则均已保留。"
    exit 0
}

if ($Verify) {
    Assert-InstalledState `
        -TargetRoot $targetRoot `
        -Payload $payload `
        -Lock $existingLock `
        -ReleaseVersion $releaseVersion
    Write-Output "受管文件：通过，共 $($payload.Count) 个。"
    Write-Output "项目版本管理忽略规则：已预先排除 .scratch，安装器未修改。"
    exit 0
}

$upgradeState = $null
if ($isUpgrade) {
    $upgradeState = Get-SupportedManagedState -Lock $existingLock -TargetRoot $targetRoot
}
$existingLockHashes = Get-LockHashMap -Lock $existingLock

$conflicts = @()
foreach ($item in $payload) {
    $destination = Join-Path $targetRoot $item.RelativePath
    if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
        continue
    }
    $destinationHash = Get-Sha256 -Path $destination
    if ($destinationHash -eq $item.Hash) {
        continue
    }
    if (
        -not $existingLockHashes.ContainsKey($item.RelativePath) -or
        $existingLockHashes[$item.RelativePath] -ne $destinationHash
    ) {
        $conflicts += $item.RelativePath
    }
}
if ($null -ne $existingLock -and -not $isUpgrade) {
    if ($existingLockHashes.Count -ne $payload.Count) {
        throw "同版本安装锁与当前发行载荷不一致，拒绝修改。"
    }
    foreach ($item in $payload) {
        if (
            -not $existingLockHashes.ContainsKey($item.RelativePath) -or
            $existingLockHashes[$item.RelativePath] -ne $item.Hash
        ) {
            throw "同版本安装锁不能证明当前发行载荷归属，拒绝修改：$($item.RelativePath)"
        }
    }
}
if ($conflicts.Count -gt 0) {
    throw (
        "检测到无法安全处理的本地漂移或归属冲突，拒绝覆盖：`n- " +
        ($conflicts -join "`n- ")
    )
}

$runtimeRoot = Resolve-ManagedRelativePath -TargetRoot $targetRoot -RelativePath ".scratch/dloop-v3"
$historyRoot = Resolve-ManagedRelativePath -TargetRoot $targetRoot -RelativePath ".scratch/dloop-history"
$retireRuntime = ($isUpgrade -or $null -eq $existingLock) -and (Test-Path -LiteralPath $runtimeRoot)
$historyDirectory = $null
$archivedRuntime = $null
if ($retireRuntime) {
    Assert-NoReparsePoint -Path $runtimeRoot -Boundary $targetRoot
    Assert-NoReparsePoint -Path $historyRoot -Boundary $targetRoot
    if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) {
        throw "工作流活动位置不是目录：$runtimeRoot"
    }
    $historyDirectory = Join-Path $historyRoot (
        $installedVersion + "-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" + [Guid]::NewGuid().ToString("N")
    )
    $archivedRuntime = Join-Path $historyDirectory "runtime"
    if (-not (Test-PathWithin -Candidate $archivedRuntime -Root $targetRoot)) {
        throw "历史档案位置超出目标项目。"
    }
    # An unfinished job may be retired, but an executing command must first exit.
    $runtimeDirectories = @($runtimeRoot) + @(Get-ChildItem -LiteralPath $runtimeRoot -Directory | ForEach-Object { $_.FullName })
    foreach ($runtimeDirectory in $runtimeDirectories) {
        $workspaceLock = Join-Path $runtimeDirectory "outputs/.feature-archive-workspace-state.lock"
        if (Test-Path -LiteralPath $workspaceLock -PathType Leaf) {
            try {
                $stream = [System.IO.File]::Open($workspaceLock, 'Open', 'ReadWrite', 'None')
                $stream.Dispose()
            }
            catch {
                throw "旧工作区仍被命令访问或无法独占打开。请先停止旧版命令和 Agent 再升级；作业无需完成。原因：$($_.Exception.Message)"
            }
        }
    }
}

$newLock = New-LockDocument `
    -ManagedPayload $payload `
    -Release $release `
    -ReleaseVersion $releaseVersion
$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "feature-archive-install-" + [System.Guid]::NewGuid().ToString("N")
)
$snapshots = @{}
$directorySnapshots = @{}
try {
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    foreach ($item in $payload) {
        $stagedPath = Join-Path $stagingRoot $item.RelativePath
        $stagedDirectory = Split-Path -Parent $stagedPath
        New-Item -ItemType Directory -Path $stagedDirectory -Force | Out-Null
        Copy-Item -LiteralPath $item.Source -Destination $stagedPath -Force
        if ((Get-Sha256 -Path $stagedPath) -ne $item.Hash) {
            throw "暂存文件哈希校验失败：$($item.RelativePath)"
        }
    }

    if ($retireRuntime) {
        Save-DirectoryHierarchySnapshot -Snapshots $directorySnapshots -Path $historyDirectory -Boundary $targetRoot
        New-Item -ItemType Directory -Path $historyDirectory -Force | Out-Null
        Move-Item -LiteralPath $runtimeRoot -Destination $archivedRuntime
        $retirementPath = Join-Path $historyDirectory "retirement.json"
        Save-FileSnapshot -Snapshots $snapshots -Path $retirementPath
        Write-JsonDocument -Path $retirementPath -Document ([ordered]@{
            status = "retired"
            sourceVersion = $installedVersion
            replacementVersion = $releaseVersion
            retiredAt = [DateTime]::UtcNow.ToString("o")
            scope = "all-jobs-and-workspace-ownership"
            runtime = "runtime"
            codeChanges = "preserved-without-acceptance"
        })
        $noticePath = Join-Path $historyDirectory "README.md"
        Save-FileSnapshot -Snapshots $snapshots -Path $noticePath
        [System.IO.File]::WriteAllText($noticePath, (
            "# 历史工作流档案（已失效）`n`n" +
            "本批全部作业已因版本交替退出活动流程，包括未结束作业。原状态仅用于追溯，不再授予实施、批准、恢复或工作区占用资格。`n`n" +
            "正文、附件、快照及旧占用记录保留在 runtime；项目代码改动保持原样，未自动验收、回滚或提交。新作业使用项目当前活动目录。`n"
        ), [System.Text.UTF8Encoding]::new($false))
        Test-FailureInjection -Step "after-retirement"
    }
    if ($isUpgrade) {
        Remove-SupportedManagedState -TargetRoot $targetRoot -LockPath $lockPath -State $upgradeState -Snapshots $snapshots -DirectorySnapshots $directorySnapshots
    }
    foreach ($item in $payload) {
        $stagedPath = Join-Path $stagingRoot $item.RelativePath
        $destination = Join-Path $targetRoot $item.RelativePath
        Save-FileSnapshot -Snapshots $snapshots -Path $destination
        $destinationDirectory = Split-Path -Parent $destination
        Save-DirectoryHierarchySnapshot `
            -Snapshots $directorySnapshots `
            -Path $destinationDirectory `
            -Boundary $targetRoot
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        Copy-Item -LiteralPath $stagedPath -Destination $destination -Force
    }
    Test-FailureInjection -Step "after-payload"

    Save-FileSnapshot -Snapshots $snapshots -Path $lockPath
    Save-DirectoryHierarchySnapshot `
        -Snapshots $directorySnapshots `
        -Path (Split-Path -Parent $lockPath) `
        -Boundary $targetRoot
    Write-JsonDocument -Path $lockPath -Document $newLock
    Test-FailureInjection -Step "after-lock"

    $installedLock = Read-JsonFile -Path $lockPath -Description "安装锁"
    Assert-InstalledState `
        -TargetRoot $targetRoot `
        -Payload $payload `
        -Lock $installedLock `
        -ReleaseVersion $releaseVersion
}
catch {
    $failure = $_
    if ($null -ne $archivedRuntime -and (Test-Path -LiteralPath $archivedRuntime)) {
        Move-Item -LiteralPath $archivedRuntime -Destination $runtimeRoot
    }
    Restore-FileSnapshots -Snapshots $snapshots
    Restore-DirectorySnapshots -Snapshots $directorySnapshots
    throw $failure
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}

if ($retireRuntime) {
    Write-Output "旧作业已全部归档失效，工作区占用已释放：$historyDirectory"
    Write-Output "项目代码改动已保留；新版作业从空的活动档案目录开始。"
}
Write-Output "安装完成：功能交付流 $releaseVersion，共 $($payload.Count) 个受管文件。"
Write-Output "项目版本管理忽略规则已验证且未被安装器修改。"
