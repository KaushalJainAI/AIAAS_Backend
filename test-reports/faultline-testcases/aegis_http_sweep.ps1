param(
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Continue"

function Convert-Body($Body) {
    if ($null -eq $Body) { return $null }
    try { return $Body | ConvertFrom-Json } catch { return ($Body.ToString()).Substring(0, [Math]::Min(300, $Body.ToString().Length)) }
}

function Invoke-Aegis {
    param(
        [string]$Method,
        [string]$Path,
        [object]$Payload = $null,
        [hashtable]$Headers = @{},
        [int[]]$Expected = @(200,201,202,204,400,401,403,404,405)
    )

    $uri = "$BaseUrl$Path"
    $jsonPayload = $null
    $statusCode = 0
    $responseBody = $null
    $elapsed = [System.Diagnostics.Stopwatch]::StartNew()

    try {
        $params = @{
            Uri = $uri
            Method = $Method
            Headers = $Headers
            UseBasicParsing = $true
            TimeoutSec = 15
        }
        if ($null -ne $Payload) {
            $jsonPayload = $Payload | ConvertTo-Json -Depth 20
            $params["Body"] = $jsonPayload
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
    Write-Host ("AEGIS_RESULT: " + ($record | ConvertTo-Json -Depth 20 -Compress))

    if ($Expected -notcontains $statusCode) {
        throw "Unexpected status $statusCode for $Method $Path"
    }
    return $record
}

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$password = "AegisTest!23456"
$email = "aegis+$suffix@example.com"
$username = "aegis_$suffix"

$registerPayload = @{
    username = $username
    email = $email
    password = $password
    password2 = $password
    first_name = "Aegis"
    last_name = "Sweep"
}

$reg = Invoke-Aegis -Method "POST" -Path "/api/auth/register/" -Payload $registerPayload -Expected @(201,400,429,500)
$access = $null
$refresh = $null
if ($reg.status -eq 201 -and $reg.response.access) {
    $access = $reg.response.access
    $refresh = $reg.response.refresh
} else {
    $login = Invoke-Aegis -Method "POST" -Path "/api/auth/login/" -Payload @{ email = $email; password = $password } -Expected @(200,400,401,429)
    if ($login.status -eq 200) {
        $access = $login.response.access
        $refresh = $login.response.refresh
    }
}

$authHeaders = @{}
if ($access) { $authHeaders["Authorization"] = "Bearer $access" }

Invoke-Aegis -Method "POST" -Path "/api/auth/login/" -Payload @{ email = $email; password = "wrong-password" } -Expected @(400,401,429)
Invoke-Aegis -Method "GET" -Path "/api/auth/profile/" -Headers $authHeaders -Expected @(200,401,403)
Invoke-Aegis -Method "PATCH" -Path "/api/auth/profile/" -Headers $authHeaders -Payload @{ display_name = "Aegis Sweep"; bio = "{{7*7}} ;id ../" } -Expected @(200,400,401,403)
if ($refresh) {
    Invoke-Aegis -Method "POST" -Path "/api/auth/token/refresh/" -Payload @{ refresh = $refresh } -Expected @(200,400,401)
}

Invoke-Aegis -Method "GET" -Path "/api/nodes/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/nodes/categories/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/nodes/models/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/compile/validate/" -Headers $authHeaders -Payload @{ nodes = @(); edges = @(); name = "empty" } -Expected @(200,400,401,403,500)

$workflowPayload = @{
    name = "Aegis Smoke $suffix"
    description = "Faultline sweep workflow"
    nodes = @()
    edges = @()
    workflow_settings = @{}
    context = @{}
    tags = @("aegis")
}
$wf = Invoke-Aegis -Method "POST" -Path "/api/orchestrator/workflows/" -Headers $authHeaders -Payload $workflowPayload -Expected @(201,400,401,403,500)
$maliciousWorkflowPayload = $workflowPayload.Clone()
$maliciousWorkflowPayload["name"] = "../drop table users; --"
Invoke-Aegis -Method "POST" -Path "/api/orchestrator/workflows/" -Headers $authHeaders -Payload $maliciousWorkflowPayload -Expected @(400,401,403)
Invoke-Aegis -Method "GET" -Path "/api/orchestrator/workflows/" -Headers $authHeaders -Expected @(200,401,403,500)
if ($wf.status -eq 201 -and $wf.response.id) {
    $wid = $wf.response.id
    Invoke-Aegis -Method "GET" -Path "/api/orchestrator/workflows/$wid/" -Headers $authHeaders -Expected @(200,401,403,404,500)
    Invoke-Aegis -Method "POST" -Path "/api/orchestrator/workflows/$wid/validate/" -Headers $authHeaders -Expected @(200,400,401,403,404,500)
    Invoke-Aegis -Method "DELETE" -Path "/api/orchestrator/workflows/$wid/" -Headers $authHeaders -Expected @(200,204,401,403,404,405,500)
}

Invoke-Aegis -Method "GET" -Path "/api/browseros/workspaces/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/browseros/workspaces/" -Headers $authHeaders -Payload @{ name = "Aegis Workspace"; theme_preferences = @{ mode = "dark" } } -Expected @(201,400,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/browseros/notifications/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/browseros/notifications/" -Headers $authHeaders -Payload @{ title = "Aegis"; message = "probe"; type = "info"; is_read = $false } -Expected @(201,400,401,403,500)

Invoke-Aegis -Method "GET" -Path "/api/credentials/types/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/credentials/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/skills/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/skills/" -Headers $authHeaders -Payload @{ title = "Aegis Skill"; description = "probe"; content = "noop"; isShared = $false; category = "testing" } -Expected @(201,400,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/chat/sessions/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "POST" -Path "/api/chat/guest/sessions/" -Payload @{ title = "guest probe" } -Expected @(200,201,400,401,403,429,500)
Invoke-Aegis -Method "GET" -Path "/api/logs/audit/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/inference/documents/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/mcp/servers/" -Headers $authHeaders -Expected @(200,401,403,500)
Invoke-Aegis -Method "GET" -Path "/api/imagine/" -Headers $authHeaders -Expected @(200,401,403,500)

Invoke-Aegis -Method "POST" -Path "/api/auth/api-keys/" -Headers $authHeaders -Payload @{ name = "aegis-key"; expires_at = $null; is_staff = $true; role = "admin" } -Expected @(201,400,401,403,500)
