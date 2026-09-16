param(
    [Parameter(Mandatory = $true)][string]$ApiToken,
    [string]$BaseUrl = 'http://127.0.0.1:8000'
)

$ErrorActionPreference = 'Stop'
$key = 'inbound-poll-' + [guid]::NewGuid().ToString('N')
$headers = @{
    Authorization = "Bearer $ApiToken"
    'Idempotency-Key' = $key
}

$polled = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/inbound-mail/poll" -Headers $headers
$quarantine = Invoke-RestMethod -Uri "$BaseUrl/api/v1/inbound-mail/quarantine" -Headers @{
    Authorization = "Bearer $ApiToken"
}

[pscustomobject]@{
    processed = $polled.items.Count
    associated = @($polled.items | Where-Object { $_.associated }).Count
    quarantined = $quarantine.items.Count
    items = $polled.items
} | ConvertTo-Json -Depth 8
