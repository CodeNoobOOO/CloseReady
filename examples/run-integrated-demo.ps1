param(
    [Parameter(Mandatory = $true)][string]$ApiToken,
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [string]$BaseUrl = 'http://127.0.0.1:18080',
    [string]$ContactId = 'contact_demo',
    [int]$PollTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$resolvedPdf = (Resolve-Path -LiteralPath $PdfPath -ErrorAction Stop).Path
$headers = @{ Authorization = "Bearer $ApiToken" }

function New-IdempotencyKey([string]$Prefix) {
    return $Prefix + '-' + [guid]::NewGuid().ToString('N')
}

function Invoke-Mutation {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('Post', 'Patch')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)]$Body,
        [Parameter(Mandatory = $true)][string]$Key
    )
    $mutationHeaders = @{
        Authorization = "Bearer $ApiToken"
        'Idempotency-Key' = $Key
    }
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $mutationHeaders `
        -ContentType 'application/json' -Body ($Body | ConvertTo-Json -Depth 12)
}

function Get-Case([string]$CaseId) {
    return Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$CaseId" -Headers $headers
}

function Wait-Run([string]$RunId) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($PollTimeoutSeconds)
    do {
        $run = Invoke-RestMethod -Uri "$BaseUrl/api/v1/runs/$RunId" -Headers $headers
        Write-Host "Agent run status: $($run.status)"
        if ($run.status -notin @('queued', 'running')) {
            return $run
        }
        Start-Sleep -Seconds 1
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Agent run did not finish within $PollTimeoutSeconds seconds."
}

function Wait-DocumentJob([string]$CaseId, [string]$JobId) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($PollTimeoutSeconds)
    do {
        $job = Invoke-RestMethod `
            -Uri "$BaseUrl/api/v1/cases/$CaseId/document-jobs/$JobId" `
            -Headers $headers
        Write-Host "Document job status: $($job.status)"
        if ($job.status -notin @('queued', 'processing')) {
            return $job
        }
        Start-Sleep -Seconds 1
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "Document job did not finish within $PollTimeoutSeconds seconds."
}

function Assert-Equal($Actual, $Expected, [string]$Label) {
    if ($Actual -ne $Expected) {
        throw "$Label expected '$Expected' but received '$Actual'."
    }
}

Write-Host 'Checking the API...'
$ready = Invoke-RestMethod -Uri "$BaseUrl/health/ready"
Assert-Equal $ready.status 'ready' 'API readiness'

$singaporeOffset = [TimeSpan]::FromHours(8)
$nowSingapore = [DateTimeOffset]::UtcNow.ToOffset($singaporeOffset)
$dueAt = $nowSingapore.AddDays(7).ToString('o')
$promisedAt = [DateTimeOffset]::new(
    $nowSingapore.Year,
    $nowSingapore.Month,
    $nowSingapore.Day,
    17,
    0,
    0,
    $singaporeOffset
).AddDays(1)

$caseBody = @{
    client_id = 'client_demo'
    accounting_period = '2026-07'
    timezone = 'Asia/Singapore'
    owner_user_id = 'user_manager_demo'
    due_at = $dueAt
    policy_id = 'policy_demo'
    requirements = @(
        @{
            document_type = 'bank_statement'
            accounting_period = '2026-07'
            scope = @{
                entity_id = 'entity_demo'
                account_ref = 'account_demo'
                coverage_start = '2026-07-01'
                coverage_end = '2026-07-31'
            }
            completion_rule = @{
                kind = 'coverage'
                expected_item_refs = @()
                allow_multiple_documents = $true
            }
        }
    )
}

Write-Host 'Creating a fresh client-period case...'
$case = Invoke-Mutation -Method Post -Uri "$BaseUrl/api/v1/cases" `
    -Body $caseBody -Key (New-IdempotencyKey 'demo-case')
$caseId = $case.case_id
$requirementId = $case.requirements[0].requirement_id
Write-Host "Case created: $caseId"

Write-Host 'Queuing the initial agent analysis...'
$run = Invoke-Mutation -Method Post -Uri "$BaseUrl/api/v1/cases/$caseId/activate" `
    -Body @{ expected_state_version = $case.state_version } `
    -Key (New-IdempotencyKey 'demo-activate')
$run = Wait-Run $run.run_id
if ($run.status -ne 'needs_review') {
    throw "Initial analysis did not create a review task. Status=$($run.status), error=$($run.error_code)"
}

$tasks = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$caseId/review-tasks" -Headers $headers
$task = $tasks.items | Where-Object {
    $_.run_id -eq $run.run_id -and $_.status -eq 'open' -and $null -ne $_.draft
} | Select-Object -First 1
if ($null -eq $task) {
    throw 'The agent run did not produce an open customer-message draft.'
}

Write-Host 'Approving the agent draft as the assigned manager...'
$case = Get-Case $caseId
$null = Invoke-Mutation -Method Post -Uri "$BaseUrl/api/v1/cases/$caseId/review-decisions" `
    -Body @{
        expected_state_version = $case.state_version
        review_task_id = $task.review_task_id
        decision = 'approve_draft'
        reason = 'Approved for the integrated sandbox demonstration.'
    } -Key (New-IdempotencyKey 'demo-approve')

$outboxPage = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$caseId/outbox" -Headers $headers
$outbox = $outboxPage.items | Where-Object {
    $_.review_task_id -eq $task.review_task_id
} | Select-Object -First 1
if ($null -eq $outbox) {
    throw 'Approval did not create an outbox record.'
}
Assert-Equal $outbox.delivery_status 'not_attempted' 'Pre-delivery outbox status'

Write-Host 'Delivering to the local Student 3 test sink...'
$delivery = Invoke-Mutation -Method Post `
    -Uri "$BaseUrl/api/v1/cases/$caseId/outbox/$($outbox.outbox_id)/deliver" `
    -Body @{ contact_id = $ContactId } -Key (New-IdempotencyKey 'demo-deliver')
Assert-Equal $delivery.delivery_status 'sent' 'Sandbox delivery status'
Assert-Equal $delivery.live $false 'Sandbox live flag'

$mailbox = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$caseId/mailbox" -Headers $headers
if ($mailbox.items.Count -ne 1) {
    throw "Expected one sandbox mailbox message but found $($mailbox.items.Count)."
}

Write-Host 'Ingesting a synthetic reply from the approved client contact...'
$case = Get-Case $caseId
$replyBody = "I will send the complete July 2026 bank statement on $($promisedAt.ToString('dd MMMM yyyy')) at 5:00 PM Singapore time."
$replyResult = Invoke-Mutation -Method Post -Uri "$BaseUrl/api/v1/cases/$caseId/replies" `
    -Body @{
        expected_state_version = $case.state_version
        sender_email = 'client@example.test'
        received_at = $nowSingapore.ToString('o')
        body = $replyBody
        provider_message_id = 'synthetic-reply-' + [guid]::NewGuid().ToString('N')
    } -Key (New-IdempotencyKey 'demo-reply')
Assert-Equal $replyResult.associated $true 'Reply association'

Write-Host 'Asking the bounded reply agent to record the commitment...'
$case = Get-Case $caseId
$assessment = Invoke-Mutation -Method Post `
    -Uri "$BaseUrl/api/v1/cases/$caseId/replies/$($replyResult.reply.reply_id)/assess" `
    -Body @{ expected_state_version = $case.state_version } `
    -Key (New-IdempotencyKey 'demo-assess-reply')
if ($null -eq $assessment.commitment -or $null -eq $assessment.reminder) {
    throw "Reply assessment did not produce a commitment and reminder. Intent=$($assessment.finding.intent)"
}

Write-Host 'Uploading the synthetic bank statement for Student 2 processing...'
$case = Get-Case $caseId
$uploadKey = New-IdempotencyKey 'demo-document'
$rawJob = & curl.exe --silent --show-error --fail-with-body `
    --request POST "$BaseUrl/api/v1/cases/$caseId/documents" `
    --header "Authorization: Bearer $ApiToken" `
    --header "Idempotency-Key: $uploadKey" `
    --form "expected_state_version=$($case.state_version)" `
    --form "requirement_id=$requirementId" `
    --form "file=@$resolvedPdf;type=application/pdf"
if ($LASTEXITCODE -ne 0) {
    throw 'Document upload failed.'
}
$job = Wait-DocumentJob $caseId (($rawJob | ConvertFrom-Json).job_id)
Assert-Equal $job.status 'completed' 'Document job status'

$finding = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/cases/$caseId/documents/$($job.document_id)/finding" `
    -Headers $headers
$case = Get-Case $caseId
$reminders = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$caseId/reminders" -Headers $headers
$audit = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cases/$caseId/audit-events" -Headers $headers

Assert-Equal $finding.result 'satisfies' 'Document finding'
Assert-Equal $case.requirements[0].status 'accepted' 'Requirement status'
Assert-Equal $case.readiness_status 'ready_for_confirmation' 'Case readiness'

Write-Host 'Integrated demonstration completed successfully.' -ForegroundColor Green
[pscustomobject]@{
    case_id = $caseId
    agent_run_status = $run.status
    review_resolution = 'approved'
    sandbox_delivery_status = $delivery.delivery_status
    sandbox_live = $delivery.live
    reply_intent = $assessment.finding.intent
    commitment_status = $assessment.commitment.status
    reminder_status = $reminders.items[0].status
    document_job_status = $job.status
    document_finding = $finding.result
    requirement_status = $case.requirements[0].status
    readiness_status = $case.readiness_status
    audit_event_count = $audit.items.Count
} | ConvertTo-Json -Depth 8
