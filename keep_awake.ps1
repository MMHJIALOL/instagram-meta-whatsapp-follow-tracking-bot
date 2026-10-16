# Presses the Ctrl key 4 times every 30 minutes so Windows never sees the laptop as idle.
# Turn it on/off from the dashboard ("Keep laptop awake" card), or with start_keepawake.bat / stop_keepawake.bat.
# It writes keep_awake.json (pid, last/next press, heartbeat) so the dashboard can show its state.
$intervalMinutes = 30
$stateFile = Join-Path $PSScriptRoot 'keep_awake.json'

$mutex = New-Object System.Threading.Mutex($false, 'Global\ResinKeepAwake')
if (-not $mutex.WaitOne(0)) { exit }   # already running

Add-Type -Namespace Native -Name Kbd -MemberDefinition @'
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
'@

$VK_CONTROL = 0x11
$KEYUP = 0x2
$started = (Get-Date).ToUniversalTime().ToString('o')
$last = $null
$next = $null

function Save {
    $s = @{ pid = $PID; started = $started; last_press = $last; next_press = $next; beat = (Get-Date).ToUniversalTime().ToString('o') }
    try { $s | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8 } catch {}
}

while ($true) {
    for ($i = 0; $i -lt 4; $i++) {
        [Native.Kbd]::keybd_event($VK_CONTROL, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 50
        [Native.Kbd]::keybd_event($VK_CONTROL, 0, $KEYUP, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 300
    }
    $now = (Get-Date).ToUniversalTime()
    $last = $now.ToString('o')
    $next = $now.AddMinutes($intervalMinutes).ToString('o')
    while ((Get-Date).ToUniversalTime() -lt [datetime]::Parse($next).ToUniversalTime()) {
        Save
        Start-Sleep -Seconds 20
    }
}
