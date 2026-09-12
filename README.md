# mcp-picoscope

MCP-server som låter Claude styra och läsa av ett **PicoScope PS2104**
USB-oscilloskop.

> **Status: v1 byggd mot mock-backend, hårdvaran ännu inte verifierad.** Alla
> verktyg fungerar utan scope. `backends/ps2000.py` är skriven men aldrig körd —
> PicoSDK saknas på maskinen (steg 0 i [PLAN.md](PLAN.md)).

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
| PicoSDK 64-bit | Installeras separat från Pico Technology — innehåller `ps2000.dll` |
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

1. Installera **PicoSDK 64-bit** från Pico Technology.
2. Koppla in PS2104:an och bekräfta i Picos egen PicoScope-app att den ses.
   Utan drivrutin står enheten som `Status: Error` i Enhetshanteraren.
3. `pip install picosdk`.
4. `open_device(backend="ps2000")` ska nu ge modell och serienummer.

Stäng PicoScope-appen innan du använder servern — enheten kan bara öppnas av en
process i taget.

## Verktyg

| Verktyg | Beskrivning |
|---|---|
| `list_devices()` | Sök efter anslutna scope. Mocken listas alltid. |
| `open_device(backend)` | `auto` \| `ps2000` \| `mock`. `auto` faller tillbaka på mocken med tydlig varning. |
| `close_device()` | Stäng och släpp USB-enheten. |
| `get_device_info()` | Modell, serienr, drivrutin, kanaler, spänningsområden, gränser. |
| `get_server_info()` | Serverversion och exportkatalog. |
| `configure_channel(range_v, coupling, enabled)` | Kanal A. Enheten snäpper till närmaste område och svarar vilket. |
| `configure_trigger(mode, threshold_v, direction, delay_pct, auto_trigger_ms)` | Auto eller flanktrigg. |
| `configure_mock_signal(...)` | Vad mocken genererar: sine, square, ramp, triangle, noise, dc. |
| `capture_block(duration_s, samples)` | Fånga ett block → statistik + nedsamplad kurva + capture-id. |
| `measure(capture_id)` | Vpp, Vmin/Vmax, medel, RMS, frekvens, periodtid, duty cycle. |
| `export_capture(capture_id, format)` | `csv` \| `npz` \| `png` → sökväg. |
| `autoset()` | Väljer område och tidbas som visar signalen — AutoSetup-knappen. |

**Resurs:** `picoscope://state` — backend, enhet, kanal, trigg och hållna fångster.

Exporter hamnar i `./captures/`, konfigurerbart via miljövariabeln `CAPTURE_DIR`.

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
.\.venv\Scripts\python.exe -m pytest tests -q      # enhetstester + stdio-röktest
.\.venv\Scripts\python.exe tests\test_stdio.py     # bara röktestet, med utskrift
```

Mocken bär facit: analysfunktionerna testas mot signaler med känd frekvens,
amplitud och duty cycle.

Arbetsregler och fallgropar: [SOUL.md](SOUL.md), [CLAUDE.md](CLAUDE.md),
[LESSONS.md](LESSONS.md). Backlog: [TODO.md](TODO.md).
