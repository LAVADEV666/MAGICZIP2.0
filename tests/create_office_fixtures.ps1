param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$excel=$null;$book=$null;$ppt=$null;$deck=$null
try {
    $excel=New-Object -ComObject Excel.Application
    $excel.Visible=$false;$excel.DisplayAlerts=$false;$excel.EnableEvents=$false;$excel.AutomationSecurity=3
    $book=$excel.Workbooks.Add()
    $sheet=$book.Worksheets.Item(1);$sheet.Name='Datos'
    $sheet.Range('A1').Value2='DEMO - sin datos fiscales reales'
    $rows=6000
    $values=New-Object 'object[,]' $rows,2
    for($r=0;$r -lt $rows;$r++){ $values[$r,0]=[double]($r+1);$values[$r,1]='FiscalOps-'+$r }
    $sheet.Range('A2:B6001').Value2=$values
    $sheet.Range('C2:C6001').FormulaR1C1='=RC[-2]*1.16'
    $hidden=$book.Worksheets.Add();$hidden.Name='Control oculto';$hidden.Range('A1').Formula="=SUM(Datos!A2:A6001)";$hidden.Visible=0
    $excel.CalculateFull()
    $book.SaveAs((Join-Path $Destination 'Libro_con_formulas_DEMO.xlsx'),51)
    $book.Close($false);$book=$null
} finally {if($null -ne $book){$book.Close($false)};if($null -ne $excel){$excel.Quit();[void][Runtime.InteropServices.Marshal]::ReleaseComObject($excel)};[GC]::Collect();[GC]::WaitForPendingFinalizers()}
if(@(Get-Process -Name POWERPNT -ErrorAction SilentlyContinue).Count -gt 0){throw 'PowerPoint ya está abierto: no se creará una instancia de prueba que pueda interferir.'}
try {
    $ppt=New-Object -ComObject PowerPoint.Application;$ppt.AutomationSecurity=3
    $deck=$ppt.Presentations.Add(0)
    for($i=1;$i -le 2;$i++){
        $slide=$deck.Slides.Add($i,12)
        $shape=$slide.Shapes.AddTextbox(1,45,60,620,200)
        $shape.TextFrame.TextRange.Text="MAGIC ZIP 2.0 / BY LAVA`rDiapositiva $i - SOLO DATOS DE PRUEBA"
        $shape.TextFrame.TextRange.Font.Size=30
        if($i -eq 2){$slide.SlideShowTransition.Hidden=-1}
    }
    $deck.SaveAs((Join-Path $Destination 'Presentacion_con_oculta_DEMO.pptx'),24)
    $deck.Close();$deck=$null
}finally{if($null -ne $deck){$deck.Close()};if($null -ne $ppt){$ppt.Quit();[void][Runtime.InteropServices.Marshal]::ReleaseComObject($ppt)};[GC]::Collect();[GC]::WaitForPendingFinalizers()}
Write-Output 'Fixtures Office creados. No se usaron documentos personales.'
