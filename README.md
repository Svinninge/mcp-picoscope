# mcp-picoscope

MCP-server som låter Claude styra och läsa av ett **PicoScope PS2104**
USB-oscilloskop.

> **Status: v1 körd och kalibrerad mot riktig PS2104** (2026-09-12). Hela kedjan
> öppna → konfigurera → fånga → mäta → exportera fungerar mot hårdvaran, och
> både skalan (1,5 V-cell) och nollan (kortsluten ingång) är verifierade. Kvar:
> frekvens ±1 % mot en känd periodisk signal, och flanktriggen.

## Tanken

Exponera oscilloskopet som MCP-verktyg, så att man kan säga:

- "Anslut till picoscopet och visa vad som ligger på kanal A"
- "Trigga på stigande flank vid 1,5 V och fånga 10 ms"
- "Vad är frekvensen och Vpp på signalen?"
- "Spara mätningen som CSV och rita en PNG"

Servern svarar aldrig med råa sampel. En fångst är tiotusentals punkter; du får
statistik, en nedsamplad kurva (min/max per hink, så spikarna överlever) och en
sökväg till filen.

## Förutsättningar

| Krav | Not |
|---|---|
| Python 3.11+ | 64-bitars, måste matcha SDK:ns bitness |
| PicoScope PS2104 | 1 kanal, 8 bitar, legacy `ps2000`-drivrutin (**inte** `ps2000a`) |
| `ps2000.dll` 64-bit | Följer med PicoScope-appen (`winget install PicoTechnology.Picoscope.T&M`) eller med PicoSDK |
| `picosdk` | Picos officiella Python-wrappers, `pip install picosdk` |

De två sista behövs bara för riktig hårdvara. **Mock-backenden gör hela servern
körbar och testbar utan scope.**

## Installation

```powershell
git clone git@github.com:Svinninge/mcp-picoscope.git
cd mcp-picoscope
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Egen venv, inte den globala Pythonen: `mcp` 2.x drar in en nyare starlette än
vissa andra verktyg på maskinen tål.

### Registrera i Claude Code

[.mcp.json](.mcp.json) i projektroten pekar redan på venv-pythonen. Kopiera den
till det projekt du vill mäta från, eller registrera servern på användarnivå:

```powershell
claude mcp add --scope user picoscope -- C:\path\to\mcp-picoscope\.venv\Scripts\python.exe -m mcp_picoscope.server
```

### Hårdvara (steg 0)

1. Installera drivrutinen. Enklast via winget:
   `winget install PicoTechnology.Picoscope.T&M` — appen bär `ps2000.dll`.
   PicoSDK 64-bit från Pico Technology fungerar lika bra.
2. Koppla in PS2104:an. Utan drivrutin står den som `Status: Error` i
   Enhetshanteraren; med drivrutin som `PicoScope 2000 series PC Oscilloscope`.
3. `pip install picosdk`.
4. `.\.venv\Scripts\python.exe tools\step0_verify.py` skriver ut variant,
   serienummer, accepterade spänningsområden och hela timebase-tabellen.
5. `open_device(backend="ps2000")` ska nu ge modell och serienummer.

Servern hittar DLL:en själv (`_ensure_dll_on_path`). Ligger den någon annanstans,
peka ut katalogen med miljövariabeln `PICOSDK_DIR`.

Stäng PicoScope-appen innan du använder servern — enheten kan bara öppnas av en
process i taget.

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `list_devices()` | Sök efter anslutna scope. Mocken listas alltid. |
| `open_device(backend)` | `auto` \| `ps2000` \| `mock`. `auto` faller tillbaka på mocken med tydlig varning. |
| `close_device()` | Stäng och släpp USB-enheten. |
| `get_device_info()` | Modell, serienr, drivrutin, kanaler, spänningsområden, gränser. |
| `get_server_info()` | Version (`System vX.YY \| Deploy vX.YY`), exportkatalog och displayens URL. |
| `configure_channel(range_v, coupling, enabled)` | Kanal A. Enheten snäpper till närmaste område och svarar vilket. |
| `configure_trigger(mode, threshold_v, direction, delay_pct, auto_trigger_ms)` | Auto eller flanktrigg. |
| `configure_mock_signal(...)` | Vad mocken genererar: sine, square, ramp, triangle, noise, dc. |
| `capture_block(duration_s, samples)` | Fånga ett block → statistik + nedsamplad kurva + capture-id. |
| `measure(capture_id)` | Vpp, Vmin/Vmax, medel, RMS, frekvens, periodtid, duty cycle. |
| `export_capture(capture_id, format)` | `csv` \| `npz` \| `png` → sökväg. |
| `autoset()` | Väljer område och tidbas som visar signalen — AutoSetup-knappen. |
| `open_ui(force)` | Visar displayen; återanvänder fönstret som redan tittar. `force=true` ger ett extra. |

**Resurs:** `picoscope://state` — backend, enhet, kanal, trigg och hållna fångster.

Exporter hamnar i `./captures/`, konfigurerbart via miljövariabeln `CAPTURE_DIR`.

## Displayen

Första gången ett verktyg anropas startar servern en lokal sida på
`http://127.0.0.1:8071/` och öppnar den i ett **Edge-fönster i app-läge**. Den
visar kurvan som ett oscilloskop gör — rutnät, V/div, ms/div — plus mätvärden,
kanal- och trigginställningar, och en logg över vilka MCP-verktyg som anropats.
Den uppdateras var 400:e ms.

Sidan **läser** bara: den kan inte styra scopet, och en simulerad signal märks
med en orange **SIMULERAD**-flagga så att en mock aldrig kan misstas för en
mätning.

**Ett fönster.** Att sidan pollar är beviset på att ett fönster redan tittar, så
fler verktygsanrop öppnar inget nytt. Stänger du det kommer det tillbaka vid
nästa anrop. `open_ui(force=true)` ger ett extra fönster till en annan skärm.

**Skalbar.** Knapparna −/100 %/+ i huvudet zoomar hela sidan (40–200 %) och
valet kommer ihåg sig i webbläsaren — användbart på en 300 %-skalad
Windows-display, där ett "normalt" fönster fyller skärmen. Layouten fäller ihop
sig efter fönstrets storlek och tappar det minst värdefulla först: först
aktivitetsloggen, sedan inställningspanelen, sedan de sekundära mätvärdena.
Kurvan och dess V/div är sist kvar — en kurva utan skala är ingen mätning.

| Miljövariabel | Effekt |
|---|---|
| `PICOSCOPE_UI=0` | Ingen server, inget fönster. Sätt detta för obevakade körningar. |
| `PICOSCOPE_UI_PORT` | Annan startport än 8071 (tio portar provas uppåt). |

Displayen lever exakt så länge MCP-servern gör. För att titta på scopet utan en
Claude-session igång, håll en egen session öppen:

```powershell
.\.venv\Scripts\python.exe tools\ui_session.py 3600 auto
```

## Exempeldialog

> **Du:** Öppna picoscopet och kör en autoset.
>
> **Claude:** *(open_device → autoset)* Enheten öppnad. Autoset surveyade på
> ±20 V, mätte 2,0001 kHz, valde ±2 V och fångade fem perioder: Vpp 3,0 V,
> duty 30 %, RMS 1,5 V.
>
> **Du:** Spara den som PNG.
>
> **Claude:** *(export_capture)* `captures\cap0002.png`

## Utveckling

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q        # allt (hårdvarutestet hoppas över utan scope)
.\.venv\Scripts\python.exe tests\test_stdio.py       # röktest mot mock, med utskrift
.\.venv\Scripts\python.exe tests\test_hardware.py    # röktest mot riktigt scope
```

Mocken bär facit: analysfunktionerna testas mot signaler med känd frekvens,
amplitud och duty cycle. Hårdvarutestet begär `backend="ps2000"` explicit — det
får inte falla tillbaka på mocken, för då hade en trasig drivrutinssökväg lyst
grönt.

### Verifieringsverktyg

Mot riktig hårdvara, körbara var för sig:

```powershell
.\.venv\Scripts\python.exe tools\step0_verify.py        # variant, områden, timebase-tabell
.\.venv\Scripts\python.exe tools\verify_volt_scale.py   # skalan mot en känd spänning
.\.venv\Scripts\python.exe tools\verify_zero.py         # offset, kortsluten ingång
```

Arbetsregler och fallgropar: [SOUL.md](SOUL.md), [CLAUDE.md](CLAUDE.md),
[LESSONS.md](LESSONS.md). Backlog: [TODO.md](TODO.md).
