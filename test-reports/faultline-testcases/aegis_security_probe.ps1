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
            TimeoutSec = 12
        }
        if ($null -ne $Payload) {
            $params["Body"] = ($Payload | ConvertTo-Json -Depth 20)
            $params["ContentType"] = "application/json"
        }
        $resp = Invoke-WebRequest @params
        $statusCode = [int]$resp.StatusCode
        $responseBody = Convert-Body $resp.Content
        $headersOut = @{}
        foreach ($key in $resp.Headers.Keys) { $headersOut[$key] = $resp.Headers[$key] }
    } catch {
        $headersOut = @{}
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            foreach ($key in $_.Exception.Response.Headers.Keys) { $headersOut[$key] = $_.Exception.Response.Headers[$key] }
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
        security_headers = @{
            csp = $headersOut["Content-Security-Policy"]
            hsts = $headersOut["Strict-Transport-Security"]
            x_frame_options = $headersOut["X-Frame-Options"]
            nosniff = $headersOut["X-Content-Type-Options"]
            acao = $headersOut["Access-Control-Allow-Origin"]
            acac = $headersOut["Access-Control-Allow-Credentials"]
        }
        response = $responseBody
    }
    Write-Host ("AEGIS_RESULT: " + ($record | ConvertTo-Json -Depth 20 -Compress))

    if ($Expected -notcontains $statusCode) {
        throw "Unexpected status $statusCode for $Method $Path"
    }
}

$tamperedJwt = "Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJ1c2VyX2lkIjoxLCJpc19zdGFmZiI6dHJ1ZX0."
$evilOrigin = "https://evil.example"

$protectedGets = @(
    "/api/auth/profile/",
    "/api/auth/api-keys/",
    "/api/orchestrator/workflows/",
    "/api/credentials/",
    "/api/logs/audit/",
    "/api/mcp/servers/",
    "/api/browseros/workspaces/"
)

foreach ($path in $protectedGets) {
    Invoke-Aegis -Method "GET" -Path $path -Expected @(401,403)
    Invoke-Aegis -Method "GET" -Path $path -Headers @{ Authorization = $tamperedJwt } -Expected @(401,403)
}

Invoke-Aegis -Method "POST" -Path "/api/auth/login/" -Payload @{ email = "' OR 1=1--@example.com"; password = "' OR '1'='1" } -Headers @{ Origin = $evilOrigin } -Expected @(400,401,429)
Invoke-Aegis -Method "POST" -Path "/api/auth/register/" -Payload @{ username = "../admin"; email = "not-an-email"; password = "short"; password2 = "mismatch"; is_staff = $true } -Headers @{ Origin = $evilOrigin } -Expected @(400,429)
Invoke-Aegis -Method "GET" -Path "/api/health/" -Headers @{ Origin = $evilOrigin } -Expected @(200)
Invoke-Aegis -Method "GET" -Path "/api/docs/" -Headers @{ Origin = $evilOrigin } -Expected @(200)
Invoke-Aegis -Method "GET" -Path "/api/orchestrator/workflows/999999999/" -Expected @(401,403,404)
Invoke-Aegis -Method "POST" -Path "/api/chat/guest/sessions/" -Payload @{ title = "{{7*7}} ;id ../../etc/passwd" } -Headers @{ Origin = $evilOrigin } -Expected @(201,400,403,429,500)
