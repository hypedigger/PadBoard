# install_shortcut.ps1
# Ajoute "PadBoard" dans le menu Démarrer Windows

$AppName    = "PadBoard"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainScript = Join-Path $ScriptDir "main.py"

# Cherche pythonw (sans console) puis python dans l'environnement courant
$PythonExe  = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    $PythonExe = (Get-Command python3 -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) {
    Write-Error "Python introuvable. Assurez-vous que Python est dans le PATH."
    exit 1
}

# Dossier menu Démarrer de l'utilisateur courant
$StartMenuDir = [Environment]::GetFolderPath("Programs")
$ShortcutPath = Join-Path $StartMenuDir "$AppName.lnk"

$Shell    = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath       = $PythonExe
$Shortcut.Arguments        = "`"$MainScript`""
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.Description      = $AppName
$IconFile = Join-Path $ScriptDir "icon.ico"
if (Test-Path $IconFile) {
    $Shortcut.IconLocation = "$IconFile,0"
} else {
    $Shortcut.IconLocation = "$PythonExe,0"
}
$Shortcut.Save()

Write-Host "Raccourci cree : $ShortcutPath"
Write-Host "Vous pouvez maintenant chercher '$AppName' dans le menu Demarrer."
