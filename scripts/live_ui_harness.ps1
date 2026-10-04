# Disposable WinForms test window for scripts/smoke_ui_live.py.
#
# Real Win32 common controls (so the real UI Automation proxy, real window
# messages and real OLE drag-drop are exercised) inside a window nobody but the
# test owns. Every observable effect is appended to -Log as "<Tag>|<event>", so
# the test asserts on what the window actually received, not on what the skill
# reported. Closes itself after -LifetimeSec even if the test dies.

param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Log,
    [string]$Tag = "A",
    [int]$X = 100,
    [int]$Y = 100,
    [int]$LifetimeSec = 180,
    [switch]$TopMost
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Write-Event([string]$msg) {
    [System.IO.File]::AppendAllText($Log, "$Tag|$msg`n")
}

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point($X, $Y)
$form.Size = New-Object System.Drawing.Size(560, 480)
$form.TopMost = [bool]$TopMost
$form.ShowInTaskbar = $true

$script:count = 0
$countLabel = New-Object System.Windows.Forms.Label
$countLabel.Text = "Count: 0"
$countLabel.Location = New-Object System.Drawing.Point(190, 16)
$countLabel.Size = New-Object System.Drawing.Size(150, 20)

$clickBtn = New-Object System.Windows.Forms.Button
$clickBtn.Text = "Increment Counter"
$clickBtn.Location = New-Object System.Drawing.Point(10, 10)
$clickBtn.Size = New-Object System.Drawing.Size(170, 30)
$clickBtn.Add_Click({
    $script:count++
    $countLabel.Text = "Count: $script:count"
    Write-Event "click:$script:count"
})

$inputBox = New-Object System.Windows.Forms.TextBox
$inputBox.AccessibleName = "Harness Input"
$inputBox.Location = New-Object System.Drawing.Point(10, 52)
$inputBox.Size = New-Object System.Drawing.Size(320, 24)
$inputBox.Add_TextChanged({ Write-Event "text:$($inputBox.Text)" })

$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add("Harness Alpha")
[void]$menu.Items.Add("Harness Beta")
$menuCloser = New-Object System.Windows.Forms.Timer
$menuCloser.Interval = 600
$menuCloser.Add_Tick({ $menuCloser.Stop(); $menu.Close() })
$menu.Add_Opened({ Write-Event "context_opened"; $menuCloser.Start() })
$menu.Add_Closed({ Write-Event "context_closed" })

$ctxBtn = New-Object System.Windows.Forms.Button
$ctxBtn.Text = "Context Target"
$ctxBtn.Location = New-Object System.Drawing.Point(10, 90)
$ctxBtn.Size = New-Object System.Drawing.Size(150, 30)
$ctxBtn.ContextMenuStrip = $menu

# Drag starts only once the mouse moves past the system drag threshold with the
# button held (the standard WinForms pattern) — a press/release with no real
# movement in between never starts one.
$script:downAt = $null
$dragSrc = New-Object System.Windows.Forms.Button
$dragSrc.Text = "Drag Source"
$dragSrc.Location = New-Object System.Drawing.Point(190, 90)
$dragSrc.Size = New-Object System.Drawing.Size(150, 30)
$dragSrc.Add_MouseDown({
    param($s, $e)
    Write-Event "src_down:$($e.Button)"
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { $script:downAt = $e.Location }
})
$dragSrc.Add_MouseMove({
    param($s, $e)
    if ($null -ne $script:downAt -and $e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $size = [System.Windows.Forms.SystemInformation]::DragSize
        if ([Math]::Abs($e.X - $script:downAt.X) -gt ($size.Width / 2) -or
            [Math]::Abs($e.Y - $script:downAt.Y) -gt ($size.Height / 2)) {
            $script:downAt = $null
            Write-Event "drag_started"
            [void]$dragSrc.DoDragDrop("friday-payload", [System.Windows.Forms.DragDropEffects]::Copy)
        }
    }
})
$dragSrc.Add_MouseUp({ param($s, $e) Write-Event "src_up:$($e.Button)"; $script:downAt = $null })

$dropTgt = New-Object System.Windows.Forms.Button
$dropTgt.Text = "Drop Target"
$dropTgt.Location = New-Object System.Drawing.Point(370, 90)
$dropTgt.Size = New-Object System.Drawing.Size(150, 30)
$dropTgt.AllowDrop = $true
$dropTgt.Add_DragEnter({ param($s, $e) Write-Event "drop_enter"; $e.Effect = [System.Windows.Forms.DragDropEffects]::Copy })
$dropTgt.Add_DragDrop({ param($s, $e) Write-Event ("dropped:" + $e.Data.GetData([string])) })

# Sits under the window's center, which is where ui.scroll aims the wheel.
$list = New-Object System.Windows.Forms.ListBox
$list.Location = New-Object System.Drawing.Point(10, 135)
$list.Size = New-Object System.Drawing.Size(520, 290)
for ($i = 1; $i -le 120; $i++) { [void]$list.Items.Add("Line $i") }
$script:lastTop = 0
$poll = New-Object System.Windows.Forms.Timer
$poll.Interval = 80
$poll.Add_Tick({
    if ($list.TopIndex -ne $script:lastTop) {
        $script:lastTop = $list.TopIndex
        Write-Event "top:$($list.TopIndex)"
    }
})

$form.Controls.AddRange(@($clickBtn, $countLabel, $inputBox, $ctxBtn, $dragSrc, $dropTgt, $list))

$lifetime = New-Object System.Windows.Forms.Timer
$lifetime.Interval = [Math]::Max(1, $LifetimeSec) * 1000
$lifetime.Add_Tick({ $lifetime.Stop(); $form.Close() })

$form.Add_Activated({ Write-Event "activated" })
$form.Add_Deactivate({ Write-Event "deactivated" })
$form.Add_Shown({ $poll.Start(); $lifetime.Start(); Write-Event "ready" })
$form.Add_FormClosed({ Write-Event "closed" })

[void][System.Windows.Forms.Application]::Run($form)
