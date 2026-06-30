param(
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Continue"

function Convert-Body($Body) {
    if ($null -eq $Body) { return $null }
    try { return $Body | ConvertFrom-Json } catch { return ($Body.ToString()).Substring(0, [Math]::Min(500, $Body.ToString().Length)) }
}

function Invoke-Aegis {
    param(
        [string]$Method,
        [string]$Path,
        [object]$Payload = $null,
        [hashtable]$Headers = @{},
        [int[]]$Expected = @(200,201,202,204,400,401,403,404,405,429,500)
    )

    $statusCode = 0
    $responseBody = $null
    $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $params = @{
            Uri = "$BaseUrl$Path"
            Method = $Method
            Headers = $Headers
            UseBasicParsing = $true
            TimeoutSec = 20
        }
        if ($null -ne $Payload) {
            $params["Body"] = ($Payload | ConvertTo-Json -Depth 30)
            $params["ContentType"] = "application/json"
        }
        $resp = Invoke-WebRequest @params
        $statusCode = [int]$resp.StatusCode
        $responseBody = Convert-Body $resp.Content
    } catch {
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $responseBody = Convert-Body $reader.ReadToEnd()
            } catch {
                $responseBody = $_.Exception.Message
            }
        } else {
            $responseBody = $_.Exception.Message
        }
    } finally {
        $elapsed.Stop()
    }

    $record = [ordered]@{
        method = $Method
        url = $Path
        payload = $Payload
        status = $statusCode
        response_ms = $elapsed.ElapsedMilliseconds
        response = $responseBody
    }
    Write-Host ("AEGIS_RESULT: " + ($record | ConvertTo-Json -Depth 30 -Compress))

    if ($Expected -notcontains $statusCode) {
        throw "Unexpected status $statusCode for $Method $Path"
    }
    return $record
}

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$password = "AegisExt!23456"
$email = "aegis.ext+$suffix@example.com"
$username = "aegis_ext_$suffix"

$reg = Invoke-Aegis -Method "POST" -Path "/api/auth/register/" -Payload @{
    username = $username
    email = $email
    password = $password
    password2 = $password
    first_name = "Aegis"
    last_name = "Extended"
} -Expected @(201,400,429)

$access = $null
$refresh = $null
if ($reg.status -eq 201 -and $reg.response.access) {
    $access = $reg.response.access
    $refresh = $reg.response.refresh
}
if (-not $access) {
    $login = Invoke-Aegis -Method "POST" -Path "/api/auth/login/" -Payload @{ email = $email; password = $password } -Expected @(200,400,401,429)
    if ($login.status -eq 200) {
        $access = $login.response.access
        $refresh = $login.response.refresh
    }
}

$authHeaders = @{}
if ($access) { $authHeaders["Authorization"] = "Bearer $access" }

Invoke-Aegis -Method "POST" -Path "/api/auth/change-password/request-otp/" -Headers $authHeaders -Expected @(200,400,401,403,429)
Invoke-Aegis -Method "POST" -Path "/api/auth/change-password/verify-otp/" -Headers $authHeaders -Payload @{ otp_code = "000000" } -Expected @(400,401,403,404,429,500)
Invoke-Aegis -Method "POST" -Path "/api/auth/password-reset-request/" -Payload @{ email = $email } -Expected @(200,400,404,429)
Invoke-Aegis -Method "POST" -Path "/api/auth/password-reset-verify/" -Payload @{ email = $email; otp_code = "000000" } -Expected @(400,401,403,404,429,500)
Invoke-Aegis -Method "POST" -Path "/api/auth/password-reset-confirm/" -Payload @{ email = $email; verification_token = "bad-token"; new_password = "NewAegis!23456"; confirm_password = "NewAegis!23456" } -Expected @(400,401,403,404,429)

$apiKey = Invoke-Aegis -Method "POST" -Path "/api/auth/api-keys/" -Headers $authHeaders -Payload @{ name = "extended-key-$suffix"; expires_at = $null } -Expected @(201,400,401,403)
if ($apiKey.status -eq 201 -and $apiKey.response.id) {
    $keyId = $apiKey.response.id
    Invoke-Aegis -Method "GET" -Path "/api/auth/api-keys/$keyId/" -Headers $authHeaders -Expected @(200,401,403,404)
    Invoke-Aegis -Method "PATCH" -Path "/api/auth/api-keys/$keyId/" -Headers $authHeaders -Payload @{ name = "extended-key-renamed"; is_active = $false; is_staff = $true } -Expected @(200,400,401,403,404)
    Invoke-Aegis -Method "POST" -Path "/api/auth/api-keys/$keyId/rotate/" -Headers $authHeaders -Expected @(200,400,401,403,404)
}

$workspace = Invoke-Aegis -Method "GET" -Path "/api/browseros/workspaces/mine/" -Headers $authHeaders -Expected @(200,401,403)
if ($workspace.status -eq 200 -and $workspace.response.id) {
    $workspaceId = $workspace.response.id
    Invoke-Aegis -Method "GET" -Path "/api/browseros/workspaces/$workspaceId/" -Headers $authHeaders -Expected @(200,401,403,404)
    Invoke-Aegis -Method "PATCH" -Path "/api/browseros/workspaces/$workspaceId/" -Headers $authHeaders -Payload @{ theme_preferences = @{ wallpaper = "{{7*7}}"; accent = "../red" } } -Expected @(200,400,401,403,404)
}

$window = Invoke-Aegis -Method "POST" -Path "/api/browseros/windows/" -Headers $authHeaders -Payload @{
    app_id = "terminal"
    title = "Aegis Terminal"
    position_x = -999999
    position_y = 999999
    width = 0
    height = 999999
    z_index = 999999
    state_data = @{ command = ";id"; nested = @{ template = "{{7*7}}" } }
} -Expected @(201,400,401,403)
if ($window.status -eq 201 -and $window.response.id) {
    $windowId = $window.response.id
    Invoke-Aegis -Method "GET" -Path "/api/browseros/windows/$windowId/" -Headers $authHeaders -Expected @(200,401,403,404)
    Invoke-Aegis -Method "PATCH" -Path "/api/browseros/windows/$windowId/" -Headers $authHeaders -Payload @{ is_minimized = $true; width = -1; state_data = @{ payload = "../../etc/passwd" } } -Expected @(200,400,401,403,404)
    Invoke-Aegis -Method "DELETE" -Path "/api/browseros/windows/$windowId/" -Headers $authHeaders -Expected @(200,204,401,403,404)
}

$notification = Invoke-Aegis -Method "POST" -Path "/api/browseros/notifications/" -Headers $authHeaders -Payload @{
    title = "Aegis Notice"
    message = "<script>alert(1)</script>"
    type = "warning"
    is_read = $false
} -Expected @(201,400,401,403)
if ($notification.status -eq 201 -and $notification.response.id) {
    $notificationId = $notification.response.id
    Invoke-Aegis -Method "GET" -Path "/api/browseros/notifications/$notificationId/" -Headers $authHeaders -Expected @(200,401,403,404)
    Invoke-Aegis -Method "PATCH" -Path "/api/browseros/notifications/$notificationId/" -Headers $authHeaders -Payload @{ is_read = $true; type = "invalid-type" } -Expected @(200,400,401,403,404)
}
Invoke-Aegis -Method "POST" -Path "/api/browseros/notifications/mark_all_read/" -Headers $authHeaders -Expected @(200,401,403)

Invoke-Aegis -Method "GET" -Path "/api/canvas-agent/node-types/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/canvas-agent/command/" -Headers $authHeaders -Payload @{
    instruction = ""
    canvas_state = @{ nodes = @(); edges = @() }
    current_url = "http://localhost/canvas"
} -Expected @(400,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/canvas-agent/command/" -Headers $authHeaders -Payload @{
    instruction = "create a node named {{7*7}}; then run powershell"
    canvas_state = @{ nodes = @(); edges = @() }
    current_url = "http://localhost/canvas"
} -Expected @(200,400,401,403,500)

Invoke-Aegis -Method "POST" -Path "/api/buddy/context/" -Headers $authHeaders -Payload @{ command = "open terminal"; context = @{ path = "../../etc/passwd" } } -Expected @(200,400,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/buddy/action/" -Headers $authHeaders -Payload @{ command = "notify me `"{{7*7}} ;id`"" } -Expected @(200,400,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/buddy/commands/" -Headers $authHeaders -Payload @{ command = "open terminal" } -Expected @(200,400,401,403,500)

foreach ($verb in @("PUT", "PATCH", "DELETE")) {
    Invoke-Aegis -Method $verb -Path "/api/auth/login/" -Payload @{ email = $email; password = $password } -Expected @(400,401,403,405,429)
    Invoke-Aegis -Method $verb -Path "/api/chat/guest/sessions/" -Payload @{ title = "verb tamper" } -Expected @(400,401,403,405,429)
}
