# mcp-picoscope

MCP-server som låter Claude styra och läsa av ett **PicoScope PS2104**
USB-oscilloskop.

> **Status: v1 klar och verifierad mot riktig PS2104** (2026-09-12). Hela kedjan
> öppna → konfigurera → fånga → mäta → exportera fungerar mot hårdvaran. Skalan
> är mätt mot en 1,5 V-cell, nollan mot kortsluten ingång och frekvensen mot en
> 800 Hz-sinus med 0,03 % fel. Även flanktriggen är verifierad: startpunktens
> spridning faller från 31 % av Vpp till 0,7 % när den slås på. Kvar: den
> interaktiva kontrollytan i displayen (issue #1) och streaming (v2).

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
| `autoset()` | Väljer område och tidbas som visar signalen. Letar över tidbaser snabb → långsam, så allt från 50 Hz till 1 MHz hittas. |
| `open_ui(force)` | Visar displayen; återanvänder fönstret som redan tittar. `force=true` ger ett extra. |

**Resurs:** `picoscope://state` — backend, enhet, kanal, trigg och hållna fångster.

Exporter hamnar i `./captures/`, konfigurerbart via miljövariabeln `CAPTURE_DIR`.

## Displayen

Första gången ett verktyg anropas startar servern en lokal sida på
`http://127.0.0.1:8071/` och öppnar den i ett **Edge-fönster i app-läge**. Den
visar kurvan som ett oscilloskop gör — rutnät, V/div, ms/div — plus mätvärden,
kanal- och trigginställningar, och en logg över vilka MCP-verktyg som anropats.
Den uppdateras var 400:e ms.

Sidan har en **Autoset**-knapp som kör exakt samma kod som MCP-verktyget, under
samma lås — resultatet syns i aktivitetsloggen, så du och Claude ser vad den
andra gjort. I övrigt läser sidan bara; den kan inte ställa kanal eller trigg,
och aldrig något som matar ut signal. En simulerad signal märks med en orange
**SIMULERAD**-flagga så att en mock inte kan misstas för en mätning.

**Ett fönster — på hela maskinen.** Det finns ett enda PS2104 på bänken, så ett
andra fönster påstår att det finns två instrument. Att sidan pollar är beviset
på att ett fönster tittar, och det beviset skrivs till en delad fil
(`%TEMP%\mcp-picoscope-ui.json`) som **alla** serverprocesser läser innan de
startar något. Stänger du fönstret slutar pollarna, anspråket blir inaktuellt
inom sex sekunder, och nästa verktygsanrop tar tillbaka det.
`open_ui(force=true)` är den enda vägen förbi regeln.

**Fönstret städas undan.** Displayen körs i en egen Edge-profil, så ett fönster
vars server avslutats kan stängas utan att din vanliga webbläsare berörs. Det
sker när servern avslutas, och som säkerhet före varje ny start. (Sidan kan inte
stänga sig själv — Chromium vägrar `window.close()` för fönster som skriptet
inte öppnat — så den visar "Servern är borta" om den ändå blir ensam kvar.)

**Fönstret kommer ihåg sig.** Samma fil bär zoom, position och storlek till
nästa gång Edge öppnas. Den mäter också upp fönsterramen — skillnaden mellan
var vi bad Edge placera fönstret och var innehållet hamnade — så att fönstret
inte vandrar en titelrad nedåt för varje start.

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
| `PICOSCOPE_UI_BROWSER=0` | Servera sidan men öppna aldrig ett fönster — för egen flik, eller för tester. |

### Tidbasen följer signalen

Ett fast fönster fungerar inte: 20 ms av en 800 Hz-sinus är 16 perioder och
läses fint, medan samma 20 ms av 11,8 kHz är 248 perioder och ritas som ett
grönt block — tre pixlar per period, vilket varken den här displayen eller ett
bänkscope kan visa upplöst. `tools/ui_session.py` sätter därför fönstret efter
uppmätt frekvens, ungefär tio perioder, och ändrar det bara när det är fel med
mer än 1,5× så att tidbasen inte hoppar på sista siffran.

**Autoset letar snabb → långsam.** En för snabb tidbas visar för få flanker och
förkastas för att den inget säger; en för långsam **aliasar** och förkastas för
att den ljuger — fel riktning fick en 11,8 kHz-sinus att rapporteras som stabila
406 Hz. En frekvens tros bara när samplingstakten är minst tio gånger den.

Displayen lever exakt så länge MCP-servern gör. För att titta på scopet utan en
Claude-session igång, håll en egen session öppen:

```powershell
.\.venv\Scripts\python.exe tools\ui_session.py 3600 auto
```

## Exempeldialog

> **Du:** Öppna picoscopet och kör en autoset.
>
> **Claude:** *(open_device → autoset)* Enheten öppnad. Autoset surveyade på
> ±20 V över 0,02 och 0,2 ms, mätte 11,807 kHz, valde ±5 V och fångade fem
> perioder: Vpp 3,02 V, duty 49,8 %, RMS 1,78 V.
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
