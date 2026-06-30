$ErrorActionPreference = "Continue"
$Base = "http://localhost:8000"
$Results = @()
function Add-Result($Method, $Url, $Payload, $Status, $Response) { $script:Results += [pscustomobject]@{method=$Method;url=$Url;payload=$Payload;status=$Status;response=$Response}; Write-Output ("AEGIS_RESULT: " + (@{method=$Method;url=$Url;payload=$Payload;status=$Status;response=$Response} | ConvertTo-Json -Compress -Depth 8)) }
function Invoke-Probe($Method,$Path,$Payload=$null,$Headers=@{}) { try { $p=@{Method=$Method;Uri="$Base$Path";UseBasicParsing=$true;TimeoutSec=15;Headers=$Headers}; if($null-ne $Payload){$p.Body=($Payload|ConvertTo-Json -Depth 10);$p.ContentType='application/json'}; $r=Invoke-WebRequest @p; Add-Result $Method $Path $Payload $r.StatusCode $r.Content; return $r } catch { $s=$null;$b=$_.Exception.Message; if($_.Exception.Response){$s=[int]$_.Exception.Response.StatusCode; try{$reader=New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream());$b=$reader.ReadToEnd()}catch{}}; Add-Result $Method $Path $Payload $s $b; return $null } }
function New-User($prefix) { $stamp=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); $email="$prefix`_$stamp@example.com"; $pw='AegisPass123!'; $r=Invoke-Probe POST '/api/auth/register/' @{username="$prefix`_$stamp";email=$email;password=$pw;password2=$pw}; $tok=$null; if($r){try{$tok=($r.Content|ConvertFrom-Json).access}catch{}}; return @{email=$email;password=$pw;headers=@{Authorization="Bearer $tok"}} }
$u1=New-User 'owner'; Start-Sleep -Milliseconds 200; $u2=New-User 'other'
$key=Invoke-Probe POST '/api/auth/api-keys/' @{name='secret-check'} $u1.headers
$keyId=$null; if($key){try{$keyId=($key.Content|ConvertFrom-Json).data.id}catch{}}
if($keyId){ Invoke-Probe GET "/api/auth/api-keys/$keyId/" $null $u1.headers; Invoke-Probe GET "/api/auth/api-keys/$keyId/" $null $u2.headers; Invoke-Probe PUT "/api/auth/api-keys/$keyId/" @{name='x';key='attacker';key_prefix='attacker';is_active=$true} $u1.headers }
Invoke-Probe POST '/api/credentials/' @{name='cred-secret';credential_type=1;data=@{apiKey='sk-test-secret-1234567890';baseUrl='https://example.invalid'}} $u1.headers
Invoke-Probe GET '/api/credentials/' $null $u1.headers
Invoke-Probe POST '/api/auth/register/' @{username='bad';email='not-an-email';password='x';password2='y'}
Invoke-Probe PATCH '/api/auth/profile/' @{credits_remaining=-1;default_temperature=999;llm_credential_id='../../etc/passwd'} $u1.headers
$Results | ConvertTo-Json -Depth 12 | Set-Content .\testcases\aegis_followup_results.json
