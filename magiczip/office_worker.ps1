param([Parameter(Mandatory=$true)][string]$RequestFile)
$ErrorActionPreference = 'Stop'
$request = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8 | ConvertFrom-Json
$application = $null
$document = $null
$blank = $null
$owned = $false
$result = $null
$missing = [Type]::Missing
$ownerPath = [string]$request.target + '.owner.json'
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class MagicOfficeWindow { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid); }'

function Record-Owner($app,[string]$processName) {
    [uint32]$nativeId=0
    [void][MagicOfficeWindow]::GetWindowThreadProcessId([IntPtr]$app.HWND,[ref]$nativeId)
    if ($nativeId -eq 0) { throw 'No se pudo identificar una instancia de Office aislada.' }
    if ($existingIds -contains [int]$nativeId) { throw 'Office devolvió una instancia que ya estaba abierta; se conserva el original.' }
    $nativeProcess=Get-Process -Id $nativeId
    @{pid=$nativeId;name=$processName;created=$nativeProcess.StartTime.ToFileTimeUtc().ToString()} | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8
}

function Cell-Key($value) {
    if ($null -eq $value) { return 'NULL' }
    if ($value -is [double]) { return 'D:' + $value.ToString('R',[Globalization.CultureInfo]::InvariantCulture) }
    return $value.GetType().FullName + ':' + [string]$value
}

function Matrix-Hash($matrix,[int]$rows,[int]$cols) {
    $builder = New-Object Text.StringBuilder
    if ($matrix -is [Array] -and $matrix.Rank -eq 2) {
        for ($r=$matrix.GetLowerBound(0);$r -le $matrix.GetUpperBound(0);$r++) {
            for ($c=$matrix.GetLowerBound(1);$c -le $matrix.GetUpperBound(1);$c++) {
                $text = Cell-Key $matrix.GetValue($r,$c)
                [void]$builder.Append($text.Length).Append(':').Append($text)
            }
        }
    } else { [void]$builder.Append((Cell-Key $matrix)) }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($builder.ToString()))) }
    finally { $sha.Dispose() }
}

function Workbook-Snapshot($book) {
    $sheets = @()
    foreach ($sheet in $book.Worksheets) {
        $range = $sheet.UsedRange
        $rows = [int]$range.Rows.Count; $cols = [int]$range.Columns.Count
        if ([long]$rows * $cols -gt 500000) { throw 'El rango usado supera 500,000 celdas: se conserva XLSX para evitar una verificación incompleta.' }
        if ($sheet.Shapes.Count -gt 0 -or $sheet.Comments.Count -gt 0 -or $sheet.ListObjects.Count -gt 0 -or $sheet.PivotTables().Count -gt 0) { throw 'El libro contiene objetos complejos; se conserva su formato original.' }
        try { $formulas = $range.Formula2 } catch { $formulas = $range.Formula }
        $sheets += [ordered]@{name=[string]$sheet.Name;visible=[int]$sheet.Visible;address=[string]$range.Address();rows=$rows;cols=$cols;values=(Matrix-Hash $range.Value2 $rows $cols);formulas=(Matrix-Hash $formulas $rows $cols)}
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($range)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sheet)
    }
    $names=@(); foreach($name in $book.Names) { $names += [ordered]@{name=[string]$name.Name;refersTo=[string]$name.RefersTo;visible=[bool]$name.Visible}; [void][Runtime.InteropServices.Marshal]::ReleaseComObject($name) }
    return ConvertTo-Json -InputObject ([ordered]@{sheets=$sheets;names=$names;date1904=[bool]$book.Date1904}) -Depth 12 -Compress
}

try {
    if ($request.mode -eq 'excel_binary') {
        $existingIds=@(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object Id)
        $application = New-Object -ComObject Excel.Application
        Record-Owner $application 'EXCEL.EXE'
        $owned = $true
        $application.Visible = $false
        $application.DisplayAlerts = $false
        $application.EnableEvents = $false
        $application.AskToUpdateLinks = $false
        $application.AutomationSecurity = 3
        $blank = $application.Workbooks.Add()
        $application.Calculation = -4135
        $application.CalculateBeforeSave = $false
        $document = $application.Workbooks.Open($request.source,0,$true)
        $before = Workbook-Snapshot $document
        $document.CheckCompatibility = $false
        $document.SaveAs($request.target,50)
        $afterSave = Workbook-Snapshot $document
        if ($before -ne $afterSave) { throw 'La verificación detectó diferencias en celdas, valores, fórmulas, hojas o nombres. Se descarta XLSB.' }
        $document.Close($false); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document); $document=$null
        $document = $application.Workbooks.Open($request.target,0,$true)
        $reopened = Workbook-Snapshot $document
        if ($before -ne $reopened) { throw 'La copia XLSB reabierta no coincide con el libro de entrada. Se descarta.' }
        $document.SaveAs($request.roundtrip,51)
        $result = @{ok=$true;verification='native_values_formulas_reopened';snapshot=(ConvertFrom-Json $before);roundtrip=$request.roundtrip}
    } elseif ($request.mode -eq 'excel_split_values') {
        if ([IO.Path]::GetExtension($request.source).ToLowerInvariant() -ne '.xlsx') { throw 'La separación por pestañas con conversión a valores solo se habilita para XLSX; se conserva el formato original.' }
        $existingIds=@(Get-Process -Name EXCEL -ErrorAction SilentlyContinue | ForEach-Object Id)
        $application = New-Object -ComObject Excel.Application
        Record-Owner $application 'EXCEL.EXE'
        $owned = $true
        $application.Visible = $false
        $application.DisplayAlerts = $false
        $application.EnableEvents = $false
        $application.AskToUpdateLinks = $false
        $application.AutomationSecurity = 3
        $application.Calculation = -4135
        $document = $application.Workbooks.Open($request.source,0,$true)
        $files = @()
        for ($index=1; $index -le $document.Worksheets.Count; $index++) {
            $sheet = $document.Worksheets.Item($index)
            $sheetName = [string]$sheet.Name
            $sheet.Copy()
            $newBook = $application.ActiveWorkbook
            $newSheet = $newBook.Worksheets.Item(1)
            # A workbook with one copied hidden sheet cannot be opened reliably;
            # the split copy is explicitly a reviewable delivery artifact.
            $newSheet.Visible = -1
            $used = $newSheet.UsedRange
            $values = $used.Value2
            $used.Value2 = $values
            $fileName = ('sheet_{0:D4}.xlsx' -f $index)
            $filePath = Join-Path $request.target $fileName
            $newBook.CheckCompatibility = $false
            $newBook.SaveAs($filePath,51)
            $newBook.Close($false)
            $checkBook = $application.Workbooks.Open($filePath,0,$true)
            $checkSheet = $checkBook.Worksheets.Item(1)
            if ($checkBook.Worksheets.Count -ne 1 -or [string]$checkSheet.Name -ne $sheetName) { throw 'La copia separada no pudo reabrirse con la pestaña esperada.' }
            $checkBook.Close($false)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($checkSheet)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($checkBook)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($used)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($newSheet)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($newBook)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sheet)
            $files += [ordered]@{name=$fileName;worksheet=$sheetName;index=$index}
        }
        $document.Close($false); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document); $document=$null
        $result = @{ok=$true;verification='native_values_one_workbook_per_worksheet';files=$files}
    } elseif ($request.mode -eq 'word_pdf') {
        $existingIds=@(Get-Process -Name WINWORD -ErrorAction SilentlyContinue | ForEach-Object Id)
        if ($existingIds.Count -gt 0) { throw 'Word ya está abierto. Cierra Word y reintenta para convertir en una instancia aislada.' }
        $application = New-Object -ComObject Word.Application
        Record-Owner $application 'WINWORD.EXE'
        $owned = $true
        $application.Visible = $false
        $application.DisplayAlerts = 0
        try { $application.AutomationSecurity = 3 } catch {}
        $document = $application.Documents.Open($request.source,$false,$true,$false)
        # wdExportFormatPDF=17, wdExportOptimizeForPrint=0.
        $document.ExportAsFixedFormat($request.target,17,0,0)
        $result = @{ok=$true;verification='native_word_export';pdf=$request.target}
    } elseif ($request.mode -eq 'powerpoint_pdf') {
        $existingIds=@(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue | ForEach-Object Id)
        if ($existingIds.Count -gt 0) { throw 'PowerPoint ya está abierto. Cierra PowerPoint y reintenta para convertir en una instancia aislada.' }
        $application = New-Object -ComObject PowerPoint.Application
        Record-Owner $application 'POWERPNT.EXE'
        $owned = $true
        $application.AutomationSecurity = 3
        $application.DisplayAlerts = 1
        $document = $application.Presentations.Open($request.source,-1,0,0)
        $slideCount = [int]$document.Slides.Count
        $hiddenCount=0
        foreach($slide in $document.Slides){
            if($slide.SlideShowTransition.Hidden -ne 0){$hiddenCount++;$slide.SlideShowTransition.Hidden=0}
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($slide)
        }
        # All slides, including hidden slides; PDF is deliberately a static copy.
        $printRange=$document.PrintOptions.Ranges.Add(1,$slideCount)
        $document.ExportAsFixedFormat($request.target,2,1,0,1,1,-1,$printRange,4,'',$true,$true,$true,$true,$false)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($printRange)
        $result = @{ok=$true;slides=$slideCount;hidden_slides=$hiddenCount;verification='native_powerpoint_export';static_copy=$true}
    } else { throw 'Modo Office desconocido.' }
} catch {
    $result = @{ok=$false;error=$_.Exception.Message;diagnostic=$_.ScriptStackTrace}
} finally {
    if ($null -ne $document) {
        try { if ($request.mode -eq 'excel_binary') { $document.Close($false) } else { $document.Saved=-1; $document.Close() } } catch {}
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document) } catch {}
    }
    if ($null -ne $blank) { try { $blank.Close($false); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($blank) } catch {} }
    if ($null -ne $application -and $owned) { try { $application.Quit() } catch {}; try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($application) } catch {} }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
ConvertTo-Json -InputObject $result -Depth 20 | Set-Content -LiteralPath $request.response -Encoding UTF8
