$ErrorActionPreference = "Continue"
$Base = "http://localhost:8000"
$Results = @()
function Add-Result($Method, $Url, $Payload, $Status, $Response, $Headers) {
  $script:Results += [pscustomobject]@{ method=$Method; url=$Url; payload=$Payload; status=$Status; response=$Response; headers=$Headers }
  $compact = @{ method=$Method; url=$Url; payload=$Payload; status=$Status; response=$Response } | ConvertTo-Json -Compress -Depth 8
  Write-Output "AEGIS_RESULT: $compact"
}
function Invoke-Probe($Method, $Path, $Payload=$null, $Headers=@{}) {
  $uri = "$Base$Path"
  try {
    $params = @{ Method=$Method; Uri=$uri; UseBasicParsing=$true; TimeoutSec=15; Headers=$Headers }
    if ($null -ne $Payload) { $params.Body = ($Payload | ConvertTo-Json -Depth 10); $params.ContentType = "application/json" }
    $r = Invoke-WebRequest @params
    Add-Result $Method $Path $Payload $r.StatusCode $r.Content ($r.Headers | ConvertTo-Json -Compress)
    return $r
  } catch {
    $status = $null; $body = $_.Exception.Message; $hdr = "{}"
    if ($_.Exception.Response) {
      $status = [int]$_.Exception.Response.StatusCode
      try { $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream()); $body = $reader.ReadToEnd() } catch {}
      try { $hdr = $_.Exception.Response.Headers | ConvertTo-Json -Compress } catch {}
    }
    Add-Result $Method $Path $Payload $status $body $hdr
    return $null
  }
}
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$email = "aegis_$stamp@example.com"
$password = "AegisPass123!"
$regPayload = @{ username="aegis_$stamp"; email=$email; password=$password; password2=$password; first_name="Aegis"; last_name="Probe" }
$reg = Invoke-Probe POST "/api/auth/register/" $regPayload
$access = $null; $refresh = $null
if ($reg -and $reg.Content) { try { $j = $reg.Content | ConvertFrom-Json; $access = $j.access; $refresh = $j.refresh } catch {} }
$authHeaders = @{}
if ($access) { $authHeaders["Authorization"] = "Bearer $access" }
Invoke-Probe POST "/api/auth/login/" @{ email=$email; password=$password }
Invoke-Probe POST "/api/auth/login/" @{ email=$email; password="wrong" }
Invoke-Probe GET "/api/auth/profile/" $null $authHeaders
Invoke-Probe PATCH "/api/auth/profile/" @{ display_name="Aegis Probe"; tier="enterprise"; credits_remaining=999999; user=@{ email="changed_$email" } } $authHeaders
Invoke-Probe GET "/api/auth/profile/" $null $authHeaders
Invoke-Probe GET "/api/auth/api-keys/" $null $authHeaders
$keyResp = Invoke-Probe POST "/api/auth/api-keys/" @{ name="probe-key"; is_active=$true } $authHeaders
$keyId = $null
if ($keyResp -and $keyResp.Content) { try { $keyId = ($keyResp.Content | ConvertFrom-Json).data.id } catch {} }
if ($keyId) { Invoke-Probe POST "/api/auth/api-keys/$keyId/rotate/" $null $authHeaders; Invoke-Probe GET "/api/auth/api-keys/$keyId/" $null $authHeaders }
Invoke-Probe GET "/api/credentials/types/" $null $authHeaders
Invoke-Probe GET "/api/credentials/" $null $authHeaders
Invoke-Probe GET "/api/browseros/workspaces/mine/" $null $authHeaders
Invoke-Probe POST "/api/browseros/notifications/" @{ title="Probe"; message="hello"; type="info"; is_read=$false; user=1 } $authHeaders
Invoke-Probe GET "/api/browseros/notifications/" $null $authHeaders
Invoke-Probe GET "/api/schema/" $null @{ Origin="https://evil.example" }
Invoke-Probe OPTIONS "/api/auth/login/" $null @{ Origin="https://evil.example"; "Access-Control-Request-Method"="POST" }
# Small rate-limit probe: enough to observe throttle without excessive load.
for ($i=0; $i -lt 8; $i++) { Invoke-Probe POST "/api/auth/login/" @{ email=$email; password="bad$i" } }
$Results | ConvertTo-Json -Depth 12 | Set-Content .\testcases\aegis_api_probe_results.json
