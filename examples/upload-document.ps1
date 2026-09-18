param(
    [Parameter(Mandatory = $true)][string]$ApiToken,
    [Parameter(Mandatory = $true)][string]$CaseId,
    [Parameter(Mandatory = $true)][string]$RequirementId,
    [Parameter(Mandatory = $true)][int]$ExpectedStateVersion,
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [string]$BaseUrl = 'http://127.0.0.1:8000'
)

$resolvedPdf = (Resolve-Path -LiteralPath $PdfPath -ErrorAction Stop).Path
$idempotencyKey = 'document-demo-' + [guid]::NewGuid().ToString('N')
$uploadUrl = "$BaseUrl/api/v1/cases/$CaseId/documents"

$rawJob = & curl.exe --silent --show-error --fail-with-body `
    --request POST $uploadUrl `
    --header "Authorization: Bearer $ApiToken" `
    --header "Idempotency-Key: $idempotencyKey" `
    --form "expected_state_version=$ExpectedStateVersion" `
    --form "requirement_id=$RequirementId" `
    --form "file=@$resolvedPdf;type=application/pdf"
if ($LASTEXITCODE -ne 0) {
    throw "Document upload failed."
}
$job = $rawJob | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $ApiToken" }

do {
    Start-Sleep -Seconds 1
    $job = Invoke-RestMethod `
        -Uri "$BaseUrl/api/v1/cases/$CaseId/document-jobs/$($job.job_id)" `
        -Headers $headers
    Write-Host "Document job status: $($job.status)"
} while ($job.status -in @('queued', 'processing'))

$finding = $null
if ($job.status -in @('completed', 'needs_review')) {
    $finding = Invoke-RestMethod `
        -Uri "$BaseUrl/api/v1/cases/$CaseId/documents/$($job.document_id)/finding" `
        -Headers $headers
}
$currentCase = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/cases/$CaseId" `
    -Headers $headers

[pscustomobject]@{
    job = $job
    finding = $finding
    current_case = $currentCase
} | ConvertTo-Json -Depth 12
