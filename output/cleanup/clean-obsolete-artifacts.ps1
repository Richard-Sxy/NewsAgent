[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param([switch]$Apply)

# Run manually in PowerShell. Without -Apply this only verifies and previews.
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).ProviderPath.TrimEnd('\')
$manifestPath = Join-Path $PSScriptRoot '2026-10-25-obsolete-artifacts.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.file_count -ne 134 -or $manifest.files.Count -ne 134) {
    throw 'The approved 134-file scope has changed.'
}
if (-not (Test-Path -LiteralPath (Join-Path $workspace 'AGENTS.md') -PathType Leaf)) {
    throw 'Run this script from the original NewsAgent repository.'
}

function Get-CheckedFile([string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Replace('\', '/').Split('/') -contains '..') {
        throw "Unsafe relative path: $RelativePath"
    }
    $resolved = (Resolve-Path -LiteralPath (Join-Path $workspace $RelativePath)).ProviderPath
    if (-not $resolved.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target is outside NewsAgent: $RelativePath"
    }
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Target must be an ordinary file: $RelativePath"
    }
    $parent = $item.Directory
    while ($parent.FullName -ne $workspace) {
        if (-not $parent.FullName.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase) -or
            ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Linked or external parent: $RelativePath"
        }
        $parent = $parent.Parent
    }
    return $item
}

$protectedPaths = @{}
foreach ($entry in $manifest.protected_files) {
    $protectedPaths[$entry.path] = $true
    $item = Get-CheckedFile $entry.path
    if ((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "Protected file changed since review; refresh the audit first: $($entry.path)"
    }
}

# Validate the entire deletion set before deleting the first file.
$targets = foreach ($entry in $manifest.files) {
    if ($protectedPaths.ContainsKey($entry.path) -or
        $entry.path -notmatch '^(output/pdf/|tmp/pdfs/|agent/docs/|DemoMD/DEMO_SCRIPT\.md$|agent/evaluation/render_tencent_warehouse_(architecture_pdf|poster)\.py$|output/warehouse/timeline-v4-data-preview-2026-10-12\.json$)') {
        throw "Target is outside the approved categories: $($entry.path)"
    }
    $item = Get-CheckedFile $entry.path
    if ((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash -ne $entry.sha256) {
        throw "Deletion target changed since review: $($entry.path)"
    }
    $item.FullName
}
if (@($targets | Select-Object -Unique).Count -ne 134) { throw 'Duplicate deletion targets.' }

if (-not $Apply) {
    $manifest.files | Select-Object path, reason, bytes
    Write-Output 'Verified 134 targets and 877 protected files. Add -Apply to delete the reviewed targets.'
    return
}

$deleted = 0
foreach ($target in $targets) {
    if ($PSCmdlet.ShouldProcess($target, 'Delete reviewed obsolete file')) {
        Remove-Item -LiteralPath $target -Force
        $deleted++
    }
}
if ($deleted -eq 134) {
    foreach ($entry in $manifest.protected_files) {
        $item = Get-CheckedFile $entry.path
        if ((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash -ne $entry.sha256) {
            throw "Protected-file verification failed: $($entry.path)"
        }
    }
    $manifest.status = 'completed'
    $manifest | Add-Member -NotePropertyName deleted_files -NotePropertyValue $deleted -Force
    $manifest | Add-Member -NotePropertyName protected_verification -NotePropertyValue '877 files unchanged' -Force
    $manifest | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $manifestPath -Encoding utf8
}
Write-Output "Deleted $deleted of 134 files. Runtime code, tests, source data packages and the five selected PDFs are retained."
