[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DesktopCommand,
    [string]$ProjectRoot = '',
    [string]$PackagePath = '',
    [string]$HostConfig = '',
    [string]$DshDataRoot = '',
    [switch]$SkipSkill,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$explicitProjectRoot = $PSBoundParameters.ContainsKey('ProjectRoot') -and -not [string]::IsNullOrWhiteSpace($ProjectRoot)
$explicitHostConfig = $PSBoundParameters.ContainsKey('HostConfig') -and -not [string]::IsNullOrWhiteSpace($HostConfig)
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).ProviderPath }
# Optional command-line helper. Normal installation uses the App's Add plugin.
if (-not $PackagePath) {
    $PackagePath = Join-Path $PSScriptRoot 'dsh-generalrouter-0.1.5.tgz'
    if (-not (Test-Path -LiteralPath $PackagePath -PathType Leaf)) {
        $PackagePath = Join-Path (Split-Path -Parent $PSScriptRoot) 'dsh-generalrouter-0.1.5.tgz'
    }
}
$projectDirectory = [IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath $projectDirectory -PathType Container)) {
    throw 'ProjectRoot 必须是你要开发 App 的现有工作目录；无需放置 GeneralRouter 源码。'
}
$packageFile = (Get-Item -LiteralPath $PackagePath).FullName
if ([IO.Path]::GetExtension($packageFile) -ne '.tgz') { throw 'PackagePath 必须指向插件 .tgz 发布包。' }
$commandPath = (Get-Item -LiteralPath $DesktopCommand).FullName
if ([IO.Path]::GetExtension($commandPath) -ne '.cmd') { throw '请指定 Desktop 随附的 dsh.cmd。' }
$launcherText = [IO.File]::ReadAllText($commandPath)
if (-not $launcherText.Contains('DeepSeek Harness.exe') -or -not $launcherText.Contains('dsh-desktop-host')) {
    throw '指定的命令不是官方 Desktop dsh.cmd。不要使用另一套 npm dsh 管理 Desktop profile。'
}
if (-not $DshDataRoot) {
    if ($env:DSH_HOME) { $DshDataRoot = $env:DSH_HOME }
    else { $DshDataRoot = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.dsh' }
}
$dataDirectory = [IO.Path]::GetFullPath($DshDataRoot)
if (-not $HostConfig) { $HostConfig = Join-Path $dataDirectory 'generalrouter\host.local.json' }
$hostConfigPath = [IO.Path]::GetFullPath($HostConfig)
$configDirectory = Split-Path -Parent $hostConfigPath
if ($explicitHostConfig -and -not (Test-Path -LiteralPath $configDirectory -PathType Container)) { throw '显式 HostConfig 的父目录必须已存在。' }
$desktopProfile = Join-Path $dataDirectory 'profiles\desktop'
if (-not (Test-Path -LiteralPath $desktopProfile -PathType Container)) {
    throw '请先正常启动一次 Desktop 建立 profile，再从托盘完全退出；关闭窗口可能只是隐藏。'
}

# Only this marked block belongs to this installer. Other user YAML is retained.
# Unusual documents and pre-existing custom rows are left for the official
# configuration editor instead of using a different YAML parser or rewriting it.
$beginMarker = '# BEGIN GeneralRouter installer configuration'
$endMarker = '# END GeneralRouter installer configuration'
$profilePatch = Join-Path $desktopProfile 'cordis.patch.yml'
$existingPatch = if (Test-Path -LiteralPath $profilePatch -PathType Leaf) { [IO.File]::ReadAllText($profilePatch) } else { '' }
$ownedPattern = '(?ms)^' + [regex]::Escape($beginMarker) + '\r?\n.*?^' + [regex]::Escape($endMarker) + '(?:\r?\n|$)'
$withoutOwned = [regex]::Replace($existingPatch, $ownedPattern, '')
$hasOrphanMarker = $withoutOwned.Contains($beginMarker) -or $withoutOwned.Contains($endMarker)
$customRowPattern = '(?m)^\s*-\s*id\s*:\s*[''\"]?generalrouter[''\"]?\s*(?:#.*)?$'
$manualConfiguration = $hasOrphanMarker -or ($withoutOwned -match $customRowPattern) -or ($withoutOwned -match '(?m)^\s*(?:%|---|\.\.\.)')
$contentLines = @($withoutOwned -split '\r?\n' | Where-Object { $_ -notmatch '^\s*(?:#.*)?$' })
$emptyArray = $contentLines.Count -eq 1 -and $contentLines[0] -match '^\s*\[\s*\]\s*$'
if ($contentLines.Count -gt 0 -and -not $emptyArray -and $withoutOwned -notmatch '(?m)^-\s+') { $manualConfiguration = $true }
$projectScalar = if ($explicitProjectRoot) { ConvertTo-Json -InputObject ($projectDirectory.Replace('\', '/')) -Compress } else { "''" }
$hostScalar = if ($explicitHostConfig) { ConvertTo-Json -InputObject ($hostConfigPath.Replace('\', '/')) -Compress } else { "''" }
$configRow = @"
- id: generalrouter
  config:
    projectRoot: $projectScalar
    hostConfig: $hostScalar
    activeQuotaRefreshSeconds: 600
"@
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backupDirectory = Join-Path $dataDirectory ('generalrouter\install-backups\' + $stamp)

Write-Host "将安装官方 bundle 格式发布包：$packageFile"
Write-Host "本地工作目录：$projectDirectory"
Write-Host "连接配置（默认本机共用）：$hostConfigPath"
Write-Host '包内已包含匹配器、能力数据、skill 和 12 组模型调用映射。DSH App 的 provider 与子模型允许列表由你设置。'
if (-not $Install) {
    Write-Host '尚未修改 profile 或本地配置。完全退出 DSH 后，加 -Install 执行安装。'
    return
}
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
foreach ($fileName in @('package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'cordis.patch.yml')) {
    $source = Join-Path $desktopProfile $fileName
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $backupDirectory ('desktop.' + $fileName))
    }
}
$oldDshHome = $env:DSH_HOME
try {
    $env:DSH_HOME = $dataDirectory
    & $commandPath plugin --profile desktop add $packageFile
    if ($LASTEXITCODE -ne 0) { throw "Desktop 安装命令未完成，退出码 $LASTEXITCODE。备份：$backupDirectory" }
} finally {
    if ($null -eq $oldDshHome) { Remove-Item Env:DSH_HOME -ErrorAction SilentlyContinue }
    else { $env:DSH_HOME = $oldDshHome }
}
$installedPackage = Join-Path $desktopProfile 'node_modules\dsh-generalrouter'
if (-not (Test-Path -LiteralPath $installedPackage -PathType Container)) {
    throw "安装命令结束，但标准包目录不可读。保留现有配置；请在 Desktop 插件页查看安装状态。备份：$backupDirectory"
}
New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
if (-not (Test-Path -LiteralPath $hostConfigPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $installedPackage 'examples\host.example.json') -Destination $hostConfigPath
    Write-Host '已建立 host.local.json；routes 可留空，自动使用包内 12 组模型调用映射。模板不含凭据。'
} else {
    Write-Host '保留已有 host.local.json 和自定义调用映射；routes 仅作可选覆盖，无需逐项补填包内 12 组默认映射。'
}
$quotaPath = Join-Path $configDirectory 'quota.local.json'
if (-not (Test-Path -LiteralPath $quotaPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $installedPackage 'examples\quota-policy.example.json') -Destination $quotaPath
    Write-Host '已建立 quota.local.json；无需 quota group。既有文件保持原样。'
}
# SkipSkill remains accepted for older command lines; no copy is needed now.
Write-Host '包内 planner skill 由插件注册，所有工作区共用，无需复制。'
$draftPath = Join-Path $backupDirectory 'generalrouter.profile-config.yml'
[IO.File]::WriteAllText($draftPath, $configRow + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
if ($manualConfiguration) {
    Write-Host "插件包已安装；已有自定义 row 或特殊 YAML，未改写 profile。请在官方配置文件中配置 GeneralRouter：$draftPath"
} else {
    $retained = $withoutOwned
    if ($emptyArray) { $retained = [regex]::Replace($retained, '(?m)^\s*\[\s*\]\s*(?:\r?\n|$)', '') }
    if ($retained.Length -gt 0 -and -not $retained.EndsWith("`n")) { $retained += [Environment]::NewLine }
    $managed = $beginMarker + [Environment]::NewLine + $configRow + [Environment]::NewLine + $endMarker + [Environment]::NewLine
    [IO.File]::WriteAllText($profilePatch, $retained + $managed, [Text.UTF8Encoding]::new($false))
    Write-Host '已在 profile 用户层保存 GeneralRouter 自己的配置；没有修改已安装包或其他插件配置。'
}
Write-Host "安装前备份：$backupDirectory"
Write-Host '请在 DSH App 设置 provider 与原生子模型允许列表，并填写 ECPA 管理连接，再启动 Desktop 新建会话。安装器不修改这些 App 设置，未运行模型、测试或插件加载检查。'
