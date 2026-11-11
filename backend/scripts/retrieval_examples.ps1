<#
.SYNOPSIS
  Examples and a smoke test for Data API semantic search, from PowerShell.

.DESCRIPTION
  Works in Windows PowerShell 5.1 and PowerShell 7+.

  Dot-source it to get the Invoke-DataApi helper in your session:
      . .\scripts\retrieval_examples.ps1
      (Invoke-DataApi -Path 'rim_chunks/columns').Body.retrieval

  Or run every example against the local demo (python scripts\retrieval_demo.py serve):
      .\scripts\retrieval_examples.ps1 -RunAll

  Against a real API export, pass its slug, alias and key:
      .\scripts\retrieval_examples.ps1 -BaseUrl https://smarthub.example.com -Api clinical-immbot `
          -Alias my_alias -Key $env:SMARTHUB_KEY -RunAll -Query 'sterility failure'

.NOTES
  Bodies are sent as UTF-8 bytes (Windows PowerShell 5.1 would otherwise mangle
  non-ASCII text), JSON is built with ConvertTo-Json -Depth 20 (the default
  depth of 2 silently truncates nested bodies), and HTTP errors are returned as
  objects instead of exceptions so status codes can be checked.
#>
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$Api = 'retrieval-demo',
    [string]$Alias = 'rim_chunks',
    [string]$Key,
    [string]$Query = 'sterility failure in aseptic filling',
    [switch]$RunAll
)

if (-not $Key) {
    $statePath = Join-Path $PSScriptRoot '..\.retrieval_demo\state.json'
    if (Test-Path $statePath) { $Key = (Get-Content $statePath -Raw | ConvertFrom-Json).api_key }
}
$script:BaseUrl = $BaseUrl.TrimEnd('/')
$script:Api = $Api
$script:Key = $Key

function Invoke-DataApi {
    <# Calls /data-api/<Api>/<Path>. Returns [pscustomobject]@{ Status; Body } for success AND errors. #>
    param(
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [Parameter(Mandatory = $true)][string]$Path,
        $Body,
        [string]$ApiKey = $script:Key,
        [string]$ContentType = 'application/json'
    )
    $params = @{
        Method          = $Method
        Uri             = "$($script:BaseUrl)/data-api/$($script:Api)/$Path"
        Headers         = @{ 'X-API-Key' = $ApiKey }
        UseBasicParsing = $true
    }
    if ($null -ne $Body) {
        if ($Body -is [string]) { $text = $Body } else { $text = ConvertTo-Json -InputObject $Body -Depth 20 -Compress }
        $params.Body = [System.Text.Encoding]::UTF8.GetBytes($text)
        $params.ContentType = $ContentType
    }
    try {
        $response = Invoke-WebRequest @params
        $status = [int]$response.StatusCode
        $content = $response.Content
    }
    catch {
        if (-not $_.Exception.Response) { throw }      # no HTTP response at all: server not running, DNS, TLS
        $status = [int]$_.Exception.Response.StatusCode
        $content = $_.ErrorDetails.Message
    }
    if ($content -is [byte[]]) { $content = [System.Text.Encoding]::UTF8.GetString($content) }
    try { $parsed = $content | ConvertFrom-Json } catch { $parsed = $content }
    [pscustomobject]@{ Status = $status; Body = $parsed }
}

function Show-Matches {
    <# Prints /retrieve or /vector-search matches as a table. #>
    param($Items)
    $Items | ForEach-Object {
        $row = $_
        $text = $row.chunk_text
        if (-not $text -and $row.row) { $text = $row.row.chunk_text }
        if ($text -and $text.Length -gt 60) { $text = $text.Substring(0, 60) + '...' }
        [pscustomobject]@{
            rank   = $row.rank
            score  = $(if ($null -ne $row.score) { [math]::Round([double]$row.score, 4) } else { $null })
            dense  = $(if ($null -ne $row.dense_score) { [math]::Round([double]$row.dense_score, 3) } elseif ($null -ne $row.similarity) { [math]::Round([double]$row.similarity, 3) } else { $null })
            sparse = $(if ($null -ne $row.sparse_score) { [math]::Round([double]$row.sparse_score, 3) } else { $null })
            text   = $text
        }
    } | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
}

if (-not $RunAll) { return }

if (-not $Key) { throw 'No API key: pass -Key, or run python scripts\retrieval_demo.py setup first.' }
$results = New-Object System.Collections.Generic.List[object]
function Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $results.Add([pscustomobject]@{ Result = $(if ($Ok) { 'PASS' } else { 'FAIL' }); Check = $Name; Detail = $Detail })
    $color = $(if ($Ok) { 'Green' } else { 'Red' })
    Write-Host ("[{0}] {1}  {2}" -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Name, $Detail) -ForegroundColor $color
}

Write-Host "`n1. Discovery: what can this alias do?" -ForegroundColor Cyan
$r = Invoke-DataApi -Path "$Alias/columns"
Check 'discovery' ($r.Status -eq 200 -and $null -ne $r.Body.retrieval) ("modes: " + ($r.Body.retrieval.modes -join ', ') + "; text_query_columns: " + ($r.Body.text_query_columns -join ', '))
$vectorColumn = $r.Body.retrieval.vector_column

Write-Host "`n2. Plain-language question on the existing /vector-search" -ForegroundColor Cyan
$r = Invoke-DataApi -Method POST -Path "$Alias/vector-search" -Body @{ vector_column = $vectorColumn; query_text = $Query; top_k = 3 }
Check 'vector-search with query_text' ($r.Status -eq 200 -and $r.Body.query_source -eq 'text') ("model: " + $r.Body.embedding_model)
if ($r.Status -eq 200) { Show-Matches $r.Body.matches }

Write-Host "`n3. The original raw-vector search still works" -ForegroundColor Cyan
$first = Invoke-DataApi -Method POST -Path "$Alias/vector-search" -Body @{ vector_column = $vectorColumn; query_text = $Query; top_k = 1; columns = @('chunk_id', $vectorColumn) }
$vector = $first.Body.matches[0].row.$vectorColumn
if ($vector -is [string]) { $vector = $vector | ConvertFrom-Json }
$r = Invoke-DataApi -Method POST -Path "$Alias/vector-search" -Body @{ vector_column = $vectorColumn; query_vector = @($vector); top_k = 1; search_mode = 'auto' }
Check 'vector-search with query_vector' ($r.Status -eq 200 -and $r.Body.query_source -eq 'vector' -and $r.Body.matches[0].distance -lt 0.0001) ("top distance: " + $r.Body.matches[0].distance)

Write-Host "`n4. Hybrid retrieval (meaning + keywords) with the alias's response template" -ForegroundColor Cyan
$r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body @{ retrieval_queries = @(@{ query = $Query; k = 5 }) }
$m = @($r.Body.results[0].matches)
Check 'retrieve hybrid' ($r.Status -eq 200 -and $m.Count -gt 0 -and $m[0].rank -eq 1) ("template: " + $r.Body.response_contract + "; matches: " + $m.Count)
if ($r.Status -eq 200) { Show-Matches $m; Write-Host 'First match as JSON:'; $m[0] | ConvertTo-Json -Depth 10 | Write-Host }

if ($Api -eq 'retrieval-demo') {
    Write-Host "`n5. Exact identifier (found by the keyword search)" -ForegroundColor Cyan
    $r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body @{ retrieval_queries = @(@{ query = 'BLA 761207'; k = 3 }) }
    $top = $r.Body.results[0].matches[0]
    Check 'identifier' ($r.Status -eq 200 -and $top.chunk_text -like '*BLA 761207*') ("rank 1: " + $top.chunk_text.Substring(0, 30))

    Write-Host "`n6. Batch: filter, keyword-only and meaning-only in one call" -ForegroundColor Cyan
    $r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body @{ retrieval_queries = @(
            @{ query = 'complete response letter'; k = 3; where = @('region:eq:EU') },
            @{ query = 'nitrosamine'; k = 2; mode = 'sparse' },
            @{ query = 'shelf life stability'; k = 2; mode = 'dense' }) }
    $sets = @($r.Body.results)
    $euOnly = @($sets[0].matches | Where-Object { $_.rimdocs_metadata.jj_region_country -ne 'EU' }).Count -eq 0
    $sparseOnly = @($sets[1].matches | Where-Object { $null -ne $_.dense_score }).Count -eq 0
    $denseOnly = @($sets[2].matches | Where-Object { $null -ne $_.sparse_score }).Count -eq 0
    Check 'batch, filter and modes' ($r.Status -eq 200 -and $sets.Count -eq 3 -and $euOnly -and $sparseOnly -and $denseOnly) ("result sets: " + $sets.Count)

    Write-Host "`n7. A second alias over the same table: shared model, its own settings" -ForegroundColor Cyan
    $r = Invoke-DataApi -Method POST -Path 'rim_chunks_b/retrieve' -Body @{ retrieval_queries = @(@{ query = $Query; k = 2 }) }
    Check 'shared registration' ($r.Status -eq 200 -and $null -ne $r.Body.results[0].matches[0].row) ("model: " + $r.Body.embedding_model + "; default row shape")

    Write-Host "`n8. Refusals (each must be rejected)" -ForegroundColor Cyan
    $r = Invoke-DataApi -Method POST -Path 'rim_chunks_narrow/retrieve' -Body @{ retrieval_queries = @(@{ query = 'x'; where = @('region:eq:EU') }) }
    Check 'hidden column filter -> 400' ($r.Status -eq 400) $r.Body.detail
    $r = Invoke-DataApi -Method POST -Path 'rim_chunks_b/retrieve' -Body @{ search_mode = 'exact'; retrieval_queries = @(@{ query = 'x'; mode = 'dense' }) }
    Check 'exact search not allowed -> 409' ($r.Status -eq 409) $r.Body.detail
}

$r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body @{ retrieval_queries = @(@{ query = 'x'; k = 101 }) }
Check 'k out of range -> 400' ($r.Status -eq 400) $r.Body.detail
$r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body 'x' -ContentType 'text/plain'
Check 'not JSON -> 415' ($r.Status -eq 415) $r.Body.detail
$r = Invoke-DataApi -Method POST -Path "$Alias/retrieve" -Body @{ retrieval_queries = @(@{ query = 'x' }) } -ApiKey 'wrong-key'
Check 'wrong API key -> 401' ($r.Status -eq 401) $r.Body.detail

$failed = @($results | Where-Object { $_.Result -eq 'FAIL' }).Count
Write-Host ("`n{0} of {1} checks passed." -f ($results.Count - $failed), $results.Count) -ForegroundColor $(if ($failed) { 'Red' } else { 'Green' })
if ($failed) { exit 1 }