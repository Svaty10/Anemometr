<#
.SYNOPSIS
  Nahraje firmware do čidla ANEMOMETRU přes UPDI (Arduino Uno/Nano, USB-UART převodník nebo UPDI Friend).

.DESCRIPTION
  Skript si sám stáhne nástroj avrdude (s kontrolou SHA-256) a najde port programátoru.
  Firmware anemometru se překládá z této složky (avr-gcc >= 12 v PATH nebo v C:\avr),
  případně se použije už přeložený build\firmware.hex. Firmware retenčky NIKDY nestahuje.
  Zapojení a řešení problémů: ..\docs\programovani.md

.EXAMPLE
  .\nahraj.ps1 -Test       # jen ověří spojení s čipem (přečte signaturu)
  .\nahraj.ps1 -Fuses      # nový čip: fuses + firmware
  .\nahraj.ps1             # aktualizace firmwaru (klíče párování zůstanou)
  .\nahraj.ps1 -Port COM5  # port zadaný ručně (když je připojeno víc zařízení)
  .\nahraj.ps1 -Build      # firmware vždy znovu přeložit (jinak jen když build\firmware.hex chybí)
  .\nahraj.ps1 -PripravArduino   # z Arduina Uno/Nano udělá programátor jtag2updi (pin 6)
  .\nahraj.ps1 -Diagnostika      # nahraje DIAGNOSTICKÝ firmware (test rádia a desky, ..\docs\diagnostika.md)
  .\nahraj.ps1 -Cti              # vyčte a vysvětlí výsledek diagnostiky z čidla

  Programátor se pozná sám: Arduino/převodník na pinech 0/1 (SerialUPDI), nebo Arduino
  s jtag2updi (pin 6). Ručně: -Programmer serialupdi | jtag2updi.
#>
param(
    [string]$Port = "",
    [switch]$Fuses,
    [switch]$Test,
    [switch]$Build,
    [switch]$PripravArduino,
    [switch]$SmazatParovani,
    [switch]$Diagnostika,
    [switch]$Cti,
    [int]$Baud = 115200,
    [string]$Programmer = "auto",
    [string]$ToolsDir = "C:\avr"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# any error: one clear red message instead of a PowerShell stack trace
trap {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot

$AvrdudeUrl = "https://github.com/avrdudes/avrdude/releases/download/v8.3/avrdude-v8.3-windows-x64.zip"
$AvrdudeSha = "8630c7d8bb9682a1685aaa9ad42550ae1131c9d13528f0d20975368ba615e9b0"
$Cache = Join-Path $env:LOCALAPPDATA "Anemometr"

function Get-Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() }

function Find-Tool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @($ToolsDir, $Cache)) {
        # Windows PowerShell 5.1: -Recurse on a missing folder searches the whole parent drive
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
        $f = Get-ChildItem -LiteralPath $dir -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    return $null
}

function Get-Avrdude {
    $exe = Find-Tool "avrdude"
    if ($exe) { return $exe }
    Write-Host "Stahuji avrdude 8.3 (github.com/avrdudes)…"
    New-Item -ItemType Directory -Force $Cache | Out-Null
    $zip = Join-Path $Cache "avrdude.zip"
    Invoke-WebRequest -UseBasicParsing $AvrdudeUrl -OutFile $zip
    if ((Get-Sha256 $zip) -ne $AvrdudeSha) { Remove-Item $zip; throw "Stažený avrdude nemá správný kontrolní součet – přerušeno." }
    Expand-Archive -Force $zip (Join-Path $Cache "avrdude")
    Remove-Item $zip
    $exe = Find-Tool "avrdude"
    if (-not $exe) { throw "avrdude se nepodařilo rozbalit." }
    return $exe
}

function Get-Port {
    if ($Port) { return $Port }
    # only USB serial devices (Arduino, CH340, CP2102, FTDI ...) – not built-in COM1 or Bluetooth ports
    $usb = @(Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.PNPDeviceID -like "USB*" -or $_.PNPDeviceID -like "FTDIBUS*" })
    if ($usb.Count -eq 1 -and $usb[0].Name -match "\((COM\d+)\)") {
        Write-Host "Programátor: $($usb[0].Name)"
        return $Matches[1]
    }
    if ($usb.Count -eq 0) { throw "Nenalezen žádný USB sériový port – připojte Arduino / převodník (příp. ovladač CH340)." }
    $list = ($usb | ForEach-Object { $_.Name }) -join "; "
    throw "Připojeno víc USB portů ($list) – zadejte jeden, např. .\nahraj.ps1 -Port COM5"
}

function Test-Echo([string]$p) {
    # SerialUPDI: TX and RX meet on the UPDI wire, so every byte sent must come back
    # even without the sensor. No echo = the programmer side is wired wrong.
    $sp = New-Object System.IO.Ports.SerialPort $p, $Baud, ([System.IO.Ports.Parity]::Even), 8, ([System.IO.Ports.StopBits]::Two)
    $sp.ReadTimeout = 500
    try {
        $sp.Open()
        Start-Sleep -Milliseconds 300            # some boards reset when the port opens
        $sp.DiscardInBuffer()
        $out = [byte[]](0x55, 0xAA, 0x0F)
        $sp.Write($out, 0, $out.Length)
        $got = New-Object byte[] 3
        $n = 0
        try { while ($n -lt 3) { $n += $sp.Read($got, $n, 3 - $n) } } catch [System.TimeoutException] { }
        return ($n -eq 3 -and $got[0] -eq 0x55 -and $got[1] -eq 0xAA -and $got[2] -eq 0x0F)
    } catch [System.UnauthorizedAccessException] {
        throw "Port $p je obsazený jiným programem – zavřete Arduino IDE / sériový monitor."
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
    }
}

function Invoke-Avrdude([string[]]$extra) {
    Write-Host "1/3 Hledám programátor…"
    $p = Get-Port                 # first: no download when no programmer is connected
    Write-Host "2/3 Kontroluji zapojení programátoru…"
    $prog = $Programmer
    if ($prog -eq "auto") {
        # SerialUPDI (pins 0/1) echoes every byte; an Arduino with jtag2updi does not
        if (Test-Echo $p) { $prog = "serialupdi"; Write-Host "    Arduino/převodník na pinech 0/1 (SerialUPDI)." }
        else { $prog = "jtag2updi"; Write-Host "    Zkouším Arduino s jtag2updi (pin 6)…" }
    } elseif ($prog -eq "serialupdi" -and -not (Test-Echo $p)) {
        throw ("Signál se nevrací. Zkuste odpojit drát UPDI od čidla a test spustit znovu:`n" +
               "  • pak 'Arduino OK' -> linku drží čidlo: nemá napájení (baterie / 3,3 V z Uno)`n" +
               "    nebo je drát na jiném pinu než UPDI (pin 10 ATtiny1624),`n" +
               "  • pořád nic -> zapojení Arduina: drát RESET – GND, D0 (přes odpor) a D1 spojené`n" +
               "    v jednom bodě. Klony s CH340 to často neumí – použijte 0-priprav-arduino.cmd.")
    }
    Write-Host "3/3 Komunikuji s čipem v čidle…"
    $avrdude = Get-Avrdude
    $avrArgs = @("-c", $prog, "-p", "t1624", "-P", $p, "-b", "$Baud") + $extra
    # show avrdude output and keep it for the diagnosis (stderr must not stop the script)
    $ErrorActionPreference = "Continue"
    $out = & $avrdude @avrArgs 2>&1 | ForEach-Object {
        $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" }
        if ($line -and $line -notmatch "^System\.Management\.Automation") { Write-Host $line; $line }
    }
    $rc = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($rc -eq 0) { return }
    $text = $out -join "`n"
    # jtag2updi answered (RSP_... status) = programmer fine, only the target is silent
    if ($prog -eq "jtag2updi" -and $text -notmatch "RSP_|FW version") {
        throw ("Arduino neodpovídá ani jako převodník (piny 0/1), ani jako programátor jtag2updi (pin 6).`n" +
               "  • klon Uno/Nano s CH340: jednou spusťte 0-priprav-arduino.cmd (bez drátů na Arduinu),`n" +
               "    pak zapojte pin 6 přes 4,7 kΩ na UPDI,`n" +
               "  • nebo zapojení přes piny 0/1: drát RESET – GND, D0 přes odpor a D1 spojené na UPDI.")
    }
    if ($prog -eq "jtag2updi") {
        throw ("Arduino (jtag2updi) je v pořádku, ale čip v čidle neodpovídá:`n" +
               "  • drát z pinu 6 Arduina přes 4,7 kΩ na UPDI (pin 10 ATtiny1624), GND na GND,`n" +
               "  • napájení čidla (baterie, nebo 3,3 V z Arduina Uno – ne obojí),`n" +
               "  • drát RESET – GND na Arduinu NESMÍ být zapojený (jtag2updi by neběželo).")
    }
    throw ("Čip v čidle neodpovídá – Arduino je zapojené dobře, chyba je na straně ČIDLA:`n" +
           "  • napájení čidla (baterie, nebo 3,3 V z Arduina Uno – ne obojí),`n" +
           "  • UPDI drát opravdu na UPDI (pin 10 ATtiny1624) a GND na GND,`n" +
           "  • zkuste pomaleji: .\nahraj.ps1 -Test -Baud 57600")
}

function Install-Jtag2updi {
    Write-Host "Nahrávám do Arduina program jtag2updi (z Arduina bude programátor na pinu 6)…"
    Write-Host "Na Arduinu nesmí být zapojené žádné dráty (hlavně ne RESET – GND)."
    $p = Get-Port
    $avrdude = Get-Avrdude
    $hex = "tools\jtag2updi\jtag2updi-uno.hex"
    foreach ($b in @(115200, 57600)) {               # Optiboot (Uno) / old bootloader (Nano)
        & $avrdude -c arduino -p m328p -P $p -b $b -U "flash:w:${hex}:i"
        if ($LASTEXITCODE -eq 0) {
            Write-Host ""
            Write-Host "Hotovo – Arduino je teď programátor jtag2updi." -ForegroundColor Green
            Write-Host "Zapojení: pin 6 → 4,7 kΩ → UPDI čidla, GND → GND, napájení čidla; RESET–GND NEzapojovat."
            return
        }
    }
    throw "Nahrání do Arduina selhalo – je to Uno/Nano (ATmega328P)? Zavřete Arduino IDE a odpojte dráty z Arduina."
}

function Get-Firmware([string]$name = "firmware",
                      [string[]]$srcs = @("main", "power", "adc", "wind", "cc1101", "crypto", "proto", "link"),
                      [string[]]$defs = @()) {
    New-Item -ItemType Directory -Force build | Out-Null
    $hex = "build\$name.hex"
    if ($Build -or -not (Test-Path $hex)) {
        $gcc = Find-Tool "avr-gcc"
        if (-not $gcc) { throw "Pro překlad je potřeba avr-gcc >= 12 (rozbalit do $ToolsDir, viz ..\docs\programovani.md)." }
        $bin = Split-Path $gcc
        $cflags = @("-mmcu=attiny1624", "-DF_CPU=3333333UL", "-Iinclude", "-Os", "-std=gnu11",
                    "-Wall", "-Wextra", "-Werror", "-Wno-array-bounds",
                    "-funsigned-char", "-funsigned-bitfields", "-fshort-enums",
                    "-ffunction-sections", "-fdata-sections", "-flto") + $defs
        $objs = @()
        foreach ($s in $srcs) {
            $obj = if ($name -eq "firmware") { "build\$s.o" } else { "build\$name-$s.o" }
            & $gcc @cflags -c "src\$s.c" -o $obj
            if ($LASTEXITCODE -ne 0) { throw "Překlad src\$s.c selhal." }
            $objs += $obj
        }
        & $gcc -mmcu=attiny1624 -Os -flto "-Wl,--gc-sections" @objs -o "build\$name.elf"
        if ($LASTEXITCODE -ne 0) { throw "Linkování selhalo." }
        & (Join-Path $bin "avr-objcopy.exe") -O ihex -R .eeprom -R .fuse -R .lock -R .signature "build\$name.elf" $hex
        & (Join-Path $bin "avr-size.exe") "build\$name.elf"
        return $hex
    }
    Write-Host "Použiji přeložený $hex (znovu přeložit: -Build)."
    return $hex
}

if ($PripravArduino) {
    Install-Jtag2updi
    return
}

if ($Test) {
    Invoke-Avrdude @()
    Write-Host "Spojení OK (signatura ATtiny1624 = 1E 94 2A)." -ForegroundColor Green
    return
}

# ---------------------------------------------------------------- diagnostika
# Záznam diagnostického firmwaru (src\diag.c) v EEPROM od 0xC0:
#   magic "DIAG" u32 | cycles u16 | heard_total u16 | heard_best i8 | pkt[23] | crc16
# pkt = testovací paket DIAG (0x17), stejný jako čidlo vysílá (nikdo ho nezpracovává).
function Get-Crc16([byte[]]$d, [int]$off, [int]$n) {
    $crc = 0xFFFF
    for ($i = 0; $i -lt $n; $i++) {
        $crc = $crc -bxor ([int]$d[$off + $i] -shl 8)
        for ($b = 0; $b -lt 8; $b++) {
            if ($crc -band 0x8000) { $crc = (($crc -shl 1) -bxor 0x1021) -band 0xFFFF } else { $crc = ($crc -shl 1) -band 0xFFFF }
        }
    }
    return $crc
}

function Show-Diagnostika([byte[]]$ee) {
    $i8 = { param($v) if ($v -ge 128) { $v - 256 } else { $v } }
    $u16 = { param($o) [int]$ee[$o] + 256 * [int]$ee[$o + 1] }
    $ano = { param($ok) if ($ok) { "ANO" } else { "NE" } }
    $base = 0xC0
    $magic = [System.Text.Encoding]::ASCII.GetString($ee, $base, 4)
    # klíč párování normálního firmwaru: 2 kopie po 59 B, magic "ANM1" + příznak spárováno
    $key = $false
    foreach ($a in @(0, 59)) {
        if ([System.Text.Encoding]::ASCII.GetString($ee, $a, 4) -eq "ANM1" -and $ee[$a + 4] -eq 1) { $key = $true }
    }
    Write-Host ""
    if ($magic -ne "DIAG") {
        Write-Host "V čidle není záznam diagnostiky." -ForegroundColor Yellow
        Write-Host "  • nahrajte diagnostický firmware (5-diagnostika.cmd) a nechte čidlo aspoň 30 s běžet,"
        Write-Host "  • když už nahraný je a záznam pořád chybí, čip se po startu hned restartuje (napájení!)."
        Write-Host ("Párovací klíč v EEPROM: " + $(if ($key) { "ANO (čidlo je spárované)" } else { "NE" }))
        return
    }
    $n = 34
    $crc = Get-Crc16 $ee $base ($n - 2)
    if ($crc -ne (& $u16 ($base + $n - 2))) {
        Write-Host "Záznam diagnostiky je poškozený (čtení během zápisu?) – zkuste vyčíst znovu." -ForegroundColor Yellow
        return
    }
    $cycles = & $u16 ($base + 4)
    $heardTotal = & $u16 ($base + 6)
    $heardBest = & $i8 $ee[$base + 8]
    $p = $base + 9
    $id = "{0:X4}" -f (& $u16 ($p + 1))
    $rst = $ee[$p + 5]
    $vbat = & $u16 ($p + 6)
    $temp = & $i8 $ee[$p + 8]
    $part = $ee[$p + 9]; $ver = $ee[$p + 10]; $f = $ee[$p + 11]
    $marc = $ee[$p + 12]; $fscal = $ee[$p + 13]
    $pulses = & $u16 ($p + 14)
    $heard = $ee[$p + 16]; $bad = $ee[$p + 17]
    $best = & $i8 $ee[$p + 18]; $floor = & $i8 $ee[$p + 19]
    $ltype = $ee[$p + 20]; $lid = "{0:X4}" -f (& $u16 ($p + 21))
    $so = ($f -band 0x01) -ne 0; $spi = ($f -band 0x02) -ne 0; $pll = ($f -band 0x04) -ne 0
    $gdo = ($f -band 0x08) -ne 0; $txok = ($f -band 0x10) -ne 0
    $reed = ($f -band 0x80) -ne 0
    $why = @()
    if ($rst -band 0x01) { $why += "zapnutí napájení" }
    if ($rst -band 0x02) { $why += "PODPĚTÍ (brown-out)" }
    if ($rst -band 0x08) { $why += "watchdog" }
    if ($rst -band 0x10) { $why += "software" }
    if ($rst -band 0x20) { $why += "programátor (UPDI)" }
    if ($rst -band 0x04) { $why += "pin RESET" }
    $types = @{ 0x03 = "retenčka"; 0x04 = "retenčka"; 0x05 = "retenčka"; 0x06 = "retenčka";
                0x13 = "data anemometru"; 0x14 = "párování anemometru"; 0x17 = "test jiného anemometru" }
    $tname = if ($types.ContainsKey([int]$ltype)) { $types[[int]$ltype] } else { "neznámý" }

    Write-Host ("Diagnostika čidla – poslední záznam (cyklus $cycles, běží asi {0} min)" -f [math]::Round($cycles * 6 / 60)) -ForegroundColor Cyan
    Write-Host ("  ID čidla:               $id")
    Write-Host ("  Poslední restart:       " + $(if ($why) { $why -join ", " } else { "–" }))
    Write-Host ("  Baterie:                " + $(if ($vbat -eq 0xFFFF) { "chyba měření" } else { "{0:N3} V" -f ($vbat / 1000.0) }))
    Write-Host ("  Teplota čipu:           " + $(if ($temp -eq -128) { "–" } else { "$temp °C" }))
    Write-Host ("  Párovací klíč v EEPROM: " + $(if ($key) { "ANO – hub musí mít čidlo spárované" } else { "NE – čidlo s normálním firmwarem nabízí párování" }))
    Write-Host ("  Kontakt anemometru:     " + $(if ($reed) { "sepnutý" } else { "rozepnutý" }) + ", pulzů od startu diagnostiky: $pulses")
    Write-Host "Rádio CC1101" -ForegroundColor Cyan
    Write-Host ("  Odpovídá (SO, krystal): " + (& $ano $so))
    Write-Host ("  PARTNUM / VERSION:      0x{0:X2} / 0x{1:X2}" -f $part, $ver)
    Write-Host ("  Zápis a čtení registru: " + (& $ano $spi))
    Write-Host ("  PLL zamčená:            " + (& $ano $pll) + ("  (MARCSTATE 0x{0:X2}, FSCAL1 0x{1:X2})" -f $marc, $fscal))
    Write-Host ("  Vysílání dokončeno:     " + (& $ano $txok))
    Write-Host ("  GDO0 při vysílání:      " + (& $ano $gdo))
    Write-Host "Příjem na čidle (2 s poslechu v každém cyklu)" -ForegroundColor Cyan
    Write-Host ("  Šum kanálu:             " + $(if ($floor -eq -128) { "–" } else { "$floor dBm" }))
    Write-Host ("  Zachyceno od startu:    $heardTotal paketů" + $(if ($heardTotal) { ", nejsilnější $heardBest dBm" } else { "" }))
    Write-Host ("  V posledním cyklu:      $heard (chybných $bad)" + $(if ($heard) { ", poslední: $tname (0x{0:X2}) od $lid" -f $ltype } else { "" }))

    $tips = @()
    if (-not $so) {
        $tips += "Rádio vůbec neodpovídá: napájení modulu CC1101, spoje CSn (PA4) a SO/MISO (PA2), krystal 26 MHz."
    } else {
        if (-not $spi) { $tips += "Rádio odpovídá, ale zápis/čtení registru nesedí: spoje SI/MOSI (PA1) a SCLK (PA3), studený spoj." }
        if ($part -ne 0x00 -or ($ver -ne 0x14 -and $ver -ne 0x04)) { $tips += "Neobvyklé PARTNUM/VERSION – čip nejspíš není CC1101 nebo SPI čte nesmysly." }
        if (-not $pll) { $tips += "Syntezátor se nezamkl: krystal 26 MHz (a jeho kondenzátory), napájení rádia." }
        if (-not $txok) { $tips += "Vysílání se nedokončilo: rádio zůstalo ve vysílání – napájení při zátěži (~30 mA), PLL." }
        if ($txok -and -not $gdo) { $tips += "GDO0 (PB2) se při vysílání nehnul: přerušený spoj GDO0 – čidlo pak NEPOZNÁ odpověď hubu (párování nefunguje)." }
        if ($floor -gt -80) { $tips += "Vysoký šum na kanálu ($floor dBm): rušení v okolí nebo zvlněné napájení rádia." }
        if ($heardTotal -eq 0 -and $cycles -ge 50) {
            $tips += ("Za ~5 min poslechu čidlo nezachytilo žádný paket. Pokud v dosahu vysílá jiné čidlo (druhý anemometr`n" +
                      "     každou minutu, čidla retenčky každých 5 min), nefunguje příjem: anténa, krystal, spoj GDO0 (PB2).")
        } elseif ($heardTotal -eq 0) {
            $tips += "Zatím nic nezachyceno – nechte diagnostiku běžet aspoň 5 minut a vyčtěte znovu."
        }
        if ($heardTotal -gt 0 -and $heardBest -lt -95) { $tips += "Ostatní čidla slyší jen slabě (nejvýš $heardBest dBm) – anténa nebo dosah." }
    }
    if ($rst -band 0x02) { $tips += "Restart kvůli podpětí: baterie nebo napájení z programátoru nestačí na vysílání." }
    if ($vbat -ne 0xFFFF -and $vbat -lt 3000) { $tips += "Napětí pod 3,0 V – normální firmware by se vypnul." }
    Write-Host ""
    if ($tips) {
        Write-Host "Co zkontrolovat:" -ForegroundColor Yellow
        $tips | ForEach-Object { Write-Host "  • $_" }
    } else {
        Write-Host "Rádio i deska v pořádku – čidlo vysílá i přijímá." -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "Zpět na normální firmware: 3-aktualizace-firmwaru.cmd (párování zůstane)."
}

if ($Cti) {
    New-Item -ItemType Directory -Force build | Out-Null
    $bin = Join-Path (Get-Location) "build\eeprom-diag.bin"
    if (Test-Path $bin) { Remove-Item $bin }
    Invoke-Avrdude @("-U", "eeprom:r:${bin}:r")
    Show-Diagnostika ([System.IO.File]::ReadAllBytes($bin))
    return
}

if ($Diagnostika) {
    $Port = Get-Port
    $hex = Get-Firmware "diag" @("diag", "power", "adc", "wind", "cc1101") @("-DCC1101_DIAG")
    $extra = @()
    if ($Fuses) { $extra += @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m") }
    Invoke-Avrdude ($extra + @("-U", "flash:w:${hex}:i"))
    Write-Host ""
    Write-Host "Diagnostický firmware běží: každých ~6 s otestuje rádio a desku." -ForegroundColor Green
    Write-Host "  • nechte čidlo běžet aspoň 5 minut (poslouchá ostatní čidla v dosahu),"
    Write-Host "  • výsledek z čidla: 6-precist-diagnostiku.cmd (vyčtení čidlo restartuje, test začne znovu),"
    Write-Host "  • zpět: 3-aktualizace-firmwaru.cmd – párování v čidle zůstává."
    return
}

if ($SmazatParovani) {
    # EEPROM = klíč párování a čítač rámců (obě kopie); flash ani fuses se nemění.
    # Nutné jen u čidla, jehož hub klíč ztratil (čidlo odebrané z hubu, nový hub bez zálohy).
    $ff = (@("0xff") * 256) -join ","
    Invoke-Avrdude @("-U", "eeprom:w:${ff}:m")
    Write-Host "Párování smazáno. Vyndejte a vložte baterii (nebo odpojte a připojte napájení) –" -ForegroundColor Green
    Write-Host "čidlo pak 10 minut nabízí párování (web anemometru: Čekat na čidlo)." -ForegroundColor Green
    return
}

# fuse1 BODCFG 0x04 = BOD 1,8 V v běhu, ve spánku vypnutý
# fuse2 OSCCFG 0x02 = interní 20 MHz (firmware si dělí na 3,33 MHz)
# fuse5 SYSCFG0 0xC5 = PA0 zůstává UPDI, EEPROM (klíče párování) přežije přehrání – NEMĚNIT
$FuseArgs = @("-U", "fuse1:w:0x04:m", "-U", "fuse2:w:0x02:m", "-U", "fuse5:w:0xC5:m")


$Port = Get-Port               # programmer first: no firmware download without one
$hex = Get-Firmware
$extra = @()
if ($Fuses) { $extra += $FuseArgs }
$extra += @("-U", "flash:w:${hex}:i")
Invoke-Avrdude $extra
Write-Host "Hotovo. Čidlo se restartovalo a 10 minut nabízí párování (hub: Nastavení → Čidla)." -ForegroundColor Green
