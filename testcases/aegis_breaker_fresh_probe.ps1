param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$OutFile = "testcases/aegis_breaker_fresh_results.json"
)

$ErrorActionPreference = "Continue"
$Results = @()

function Convert-ResponseBody($Body) {
    if ($null -eq $Body) { return $null }
    try { return $Body | ConvertFrom-Json } catch { return ($Body.ToString()).Substring(0, [Math]::Min(1000, $Body.ToString().Length)) }
}

function Add-Result($Method, $Path, $Payload, $Status, $Headers, $Body, $Ms) {
    $record = [ordered]@{
        method = $Method
        url = $Path
        payload = $Payload
        status = $Status
        response_ms = $Ms
        headers = $Headers
        response = $Body
    }
    $script:Results += [pscustomobject]$record
    Write-Output ("AEGIS_RESULT: " + ($record | ConvertTo-Json -Depth 30 -Compress))
}

function Invoke-Aegis($Method, $Path, $Payload = $null, $Headers = @{}) {
    $status = 0
    $body = $null
    $respHeaders = @{}
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $params = @{
            Method = $Method
            Uri = "$BaseUrl$Path"
            UseBasicParsing = $true
            TimeoutSec = 20
            Headers = $Headers
        }
        if ($null -ne $Payload) {
            $params.Body = ($Payload | ConvertTo-Json -Depth 30)
            $params.ContentType = "application/json"
        }
        $resp = Invoke-WebRequest @params
        $status = [int]$resp.StatusCode
        $body = Convert-ResponseBody $resp.Content
        foreach ($key in $resp.Headers.Keys) { $respHeaders[$key] = [string]$resp.Headers[$key] }
    } catch {
        if ($_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            foreach ($key in $_.Exception.Response.Headers.Keys) { $respHeaders[$key] = [string]$_.Exception.Response.Headers[$key] }
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $body = Convert-ResponseBody $reader.ReadToEnd()
            } catch {
                $body = $_.Exception.Message
            }
        } else {
            $body = $_.Exception.Message
        }
    } finally {
        $timer.Stop()
    }
    Add-Result $Method $Path $Payload $status $respHeaders $body $timer.ElapsedMilliseconds
    return @{ status = $status; body = $body; headers = $respHeaders }
}

$suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$password = "AegisBreak!23456"
$email = "aegis.breaker+$suffix@example.com"
$username = "aegis_breaker_$suffix"

Invoke-Aegis "GET" "/api/health/" $null @{ Origin = "https://evil.example" }
Invoke-Aegis "GET" "/api/auth/profile/"
Invoke-Aegis "GET" "/api/auth/api-keys/"

$reg = Invoke-Aegis "POST" "/api/auth/register/" @{
    username = $username
    email = $email
    password = $password
    password2 = $password
    first_name = "Aegis"
    last_name = "Breaker"
} @{ Origin = "https://evil.example" }

$access = $null
$refresh = $null
if ($reg.status -eq 201 -and $reg.body.access) {
    $access = $reg.body.access
    $refresh = $reg.body.refresh
} else {
    $login = Invoke-Aegis "POST" "/api/auth/login/" @{ email = $email; password = $password }
    if ($login.status -eq 200) {
        $access = $login.body.access
        $refresh = $login.body.refresh
    }
}

$auth = @{}
if ($access) { $auth.Authorization = "Bearer $access" }

Invoke-Aegis "POST" "/api/auth/login/" @{ email = "' OR '1'='1"; password = "' OR '1'='1" }
Invoke-Aegis "GET" "/api/auth/profile/" $null $auth
if ($refresh) { Invoke-Aegis "POST" "/api/auth/token/refresh/" @{ refresh = $refresh } }

Invoke-Aegis "POST" "/api/auth/api-keys/" @{ name = "breaker-key-$suffix"; is_staff = $true; role = "admin" } $auth
Invoke-Aegis "POST" "/api/browseros/windows/" @{
    app_id = "terminal"
    title = "Breaker terminal"
    position_x = -999999
    position_y = 999999
    width = 0
    height = -1
    z_index = 999999
    state_data = @{ command = ";id"; template = "{{7*7}}"; path = "../../etc/passwd" }
} $auth
Invoke-Aegis "POST" "/api/browseros/notifications/" @{
    title = "Breaker notice"
    message = "<script>alert(1)</script>"
    type = "warning"
    is_read = $false
} $auth
Invoke-Aegis "POST" "/api/compile/validate/" @{ nodes = @(); edges = @(); metadata = @{ payload = "{{7*7}}" } } $auth
Invoke-Aegis "GET" "/api/orchestrator/workflows/" $null $auth

$Results | ConvertTo-Json -Depth 30 | Set-Content -Path $OutFile
