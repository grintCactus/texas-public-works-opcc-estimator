# Windows.Media.Ocr -> TSV of word boxes.  No install required: the engine ships
# with Windows.  Usage:  powershell -File win_ocr.ps1 <image.png>
param([Parameter(Mandatory=$true)][string]$Image)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

# PS 5.1 cannot await a WinRT IAsyncOperation directly; bridge it to a Task.
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await-Op($op, $type) {
    $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}

[Windows.Storage.StorageFile,                Windows.Storage,     ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,     Windows.Graphics,    ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language,             Windows.Globalization, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,                Windows.Foundation,  ContentType=WindowsRuntime] | Out-Null

$full = (Resolve-Path $Image).Path
$file = Await-Op ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$stream = Await-Op ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
$decoder = Await-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
              (New-Object Windows.Globalization.Language 'en-US'))
if ($null -eq $engine) { throw 'no en-US OCR recognizer' }

$result = Await-Op ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$sb = New-Object System.Text.StringBuilder
foreach ($line in $result.Lines) {
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        [void]$sb.AppendLine(("{0}`t{1:F1}`t{2:F1}`t{3:F1}`t{4:F1}" -f `
            $w.Text, $r.X, $r.Y, $r.Width, $r.Height))
    }
}
[Console]::Out.Write($sb.ToString())
