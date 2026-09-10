# System.Speech で 8kHz/16bit/モノラルの WAV を直接書き出す。
# 電話帯域に合わせて合成させることで、Python 側でのリサンプルを省く。
param(
    [Parameter(Mandatory=$true)][string]$InFile,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [string]$Voice = "",
    [int]$Rate = 0
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$content = [System.IO.File]::ReadAllText($InFile, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($Voice -ne "") {
        $match = $null
        foreach ($v in $synth.GetInstalledVoices()) {
            if ($v.Enabled -and $v.VoiceInfo.Name -like "*$Voice*") { $match = $v.VoiceInfo.Name; break }
        }
        if ($null -eq $match) { throw "音声『$Voice』が見つかりません。--list-voices で確認してください。" }
        $synth.SelectVoice($match)
    }
    $synth.Rate = $Rate
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        8000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($OutFile, $fmt)
    if ($content.TrimStart().StartsWith("<speak")) { $synth.SpeakSsml($content) }
    else { $synth.Speak($content) }
    $synth.SetOutputToNull()
}
finally { $synth.Dispose() }
