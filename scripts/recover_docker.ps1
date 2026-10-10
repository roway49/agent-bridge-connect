$ErrorActionPreference = 'Stop'
$bundleArguments = @($args)
$candidates = @()
if ($env:PYTHON_BIN) { $candidates = @($env:PYTHON_BIN) }
else {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('3.14','3.13','3.12','3.11','3.10')) {
            try {
            $candidate = & $launcher.Source "-$version" -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { $candidates += $candidate.Trim() }
            } catch { continue }
        }
    }
    foreach ($name in @('python.exe','python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike '*\WindowsApps\*') { $candidates += $command.Source }
    }
}
$python = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    try {
        & $candidate -c 'import sys; raise SystemExit(sys.version_info < (3,10))' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    } catch { }
}
if (-not $python) { throw 'AgentBC requires Python 3.10 or newer. Set PYTHON_BIN to a supported interpreter.' }
$wheels = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'agentbc-*.whl' -File)
if ($wheels.Count -ne 1) { throw 'Expected exactly one AgentBC wheel in this bundle.' }
& $python -c 'import sys; from pathlib import Path; p=Path(sys.argv.pop(1)); sys.path.insert(0,str(p)); from agent_bridge_connect.docker_install_recovery import main; raise SystemExit(main())' $wheels[0].FullName @bundleArguments
exit $LASTEXITCODE
