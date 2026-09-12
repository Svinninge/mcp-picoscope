# PLAN — MCP-server för PicoScope 2104

Plan för att bygga en MCP-server (Model Context Protocol) som låter Claude styra
och läsa av ett PicoScope PS2104 USB-oscilloskop.

Status: **v1 körd mot riktig hårdvara** (2026-09-12). Steg 0–6 är genomförda:
enheten identifierar sig som variant 2104 (serienr <serial>) och hela kedjan
öppna → konfigurera → fånga → mäta → exportera fungerar genom MCP-servern.
Kvar för definition of done: en mätning mot en **känd signal**, som samtidigt
verifierar voltskalan (`MAX_ADC`) och frekvensnoggrannheten. Se
[TODO.md](TODO.md).

---

## 1. Mål

En MCP-server som exponerar oscilloskopet som verktyg åt en LLM-klient
(Claude Code / Claude Desktop), så att man kan säga saker som:

- "Anslut till picoscopet och visa vad som ligger på kanal A"
- "Trigga på stigande flank vid 1,5 V och fånga 10 ms"
- "Vad är frekvensen och Vpp på signalen?"
- "Spara mätningen som CSV och rita en PNG"

Icke-mål (v1): flerkanalsscope, avancerad protokolldekodning, realtids-streaming
till GUI, signalgenerator (PS2104 saknar siggen).

---

## 2. Hårdvaruförutsättningar — PS2104

PS2104 tillhör **PicoScope 2000-serien (äldre generation)** och använder den
gamla `ps2000`-drivrutinen — **inte** `ps2000a`. Detta är den enskilt viktigaste
tekniska detaljen i hela projektet; fel API ger "unit not found".

| Egenskap | Värde (verifieras mot datablad vid uppstart) |
|---|---|
| Kanaler | 1 (kanal A) |
| Upplösning | 8 bitar |
| API/drivrutin | `ps2000.dll` (legacy) |
| Anslutning | USB, USB-matad |
| Signalgenerator | saknas |

**Att göra i steg 0:** verifiera bandbredd, maximal samplingshastighet,
buffertdjup och tillgängliga spänningsområden mot Picos datablad och mot
`ps2000_get_unit_info()` på den faktiska enheten. Hårdkoda inget som går att
fråga drivrutinen om.

### Beroenden på datorn
- **`ps2000.dll` (64-bit)** behövs. *Installerad 2026-09-12* — men inte via
  PicoSDK: `winget install PicoTechnology.Picoscope.T&M` ger PicoScope 7-appen,
  som bär samma drivrutins-DLL:er i sin programkatalog. Det räcker.
- Python-wrappern `picosdk` (Picos officiella `picosdk-python-wrappers`),
  som ctypes-bindar mot DLL:en.
- Python 3.13 finns redan (`C:\path\to\AppData\Local\Programs\Python\Python313`).

---

## 3. Arkitektur

```
Claude (MCP-klient)
        │  stdio, MCP-protokoll
        ▼
  mcp_picoscope/server.py        ← FastMCP, verktygsdefinitioner
        │
        ▼
  mcp_picoscope/scope.py         ← abstrakt scope-interface
        ├── backends/ps2000.py   ← riktig hårdvara via picosdk
        └── backends/mock.py     ← simulerad signalkälla
        │
        ▼
  mcp_picoscope/analysis.py      ← Vpp, RMS, frekvens, duty cycle
  mcp_picoscope/export.py        ← CSV / NPZ / PNG
        │
        ▼
  mcp_picoscope/ui.py            ← lokal display (HTTP 8071) + Edge-fönster
  mcp_picoscope/ui.html          ← sidan: kurva, mätvärden, MCP-aktivitet
```

**Bärande designbeslut**

1. **Mock-backend först.** Hela servern ska gå att utveckla och testa utan
   hårdvara. Mocken genererar sinus/fyrkant/brus med känd frekvens och amplitud,
   så analysfunktionerna kan enhetstestas mot facit.
2. **En enda ägd session.** Drivrutinen är inte trådsäker och enheten kan bara
   öppnas av en process. Servern håller ett `ScopeSession`-objekt och
   serialiserar alla anrop.
3. **Aldrig råa sampelmassor i svaret.** En blockfångst kan vara tiotusentals
   punkter — det spränger kontextfönstret. Verktygen returnerar sammanfattning
   (statistik, nedsamplad kurva, filsökväg), aldrig hela arrayen.
4. **Explicit state.** Kanal- och triggerinställningar sätts med egna verktyg och
   går att läsa tillbaka, så att LLM:en kan resonera om aktuellt läge.
5. **Displayen läser, styr aldrig** *(tillagt 2026-09-12)*. Sidan speglar samma
   `ScopeSession` som verktygen skriver. Ett UI som också kunde trycka på knappar
   hade behövt sessionslåset och ett svar på vem som får ändra vad mitt i en
   mätning — det är en annan produkt.

---

## 4. MCP-verktyg (v1)

| Verktyg | Beskrivning |
|---|---|
| `list_devices()` | Sök efter anslutna scope. Returnerar serienummer och modell. |
| `open_device(backend="auto")` | Öppna enheten. `auto` → ps2000, faller tillbaka på mock om ingen hittas (med tydlig varning i svaret). |
| `close_device()` | Stäng och släpp USB-enheten. |
| `get_device_info()` | Modell, serienr, drivrutinsversion, kanaler, spänningsområden. |
| `configure_channel(range_v, coupling, enabled)` | Sätt spänningsområde och AC/DC på kanal A. |
| `configure_trigger(mode, threshold_v, direction, delay, auto_trigger_ms)` | Auto / enkel trigg på flank. |
| `capture_block(duration_s, samples)` | Fånga ett block. Returnerar statistik + nedsamplad kurva + capture-id. |
| `capture_streaming(duration_s, rate)` | Långsam kontinuerlig insamling till fil. |
| `measure(capture_id)` | Vpp, Vmin, Vmax, medel, RMS, frekvens, periodtid, duty cycle. |
| `export_capture(capture_id, format)` | `csv` \| `npz` \| `png`. Returnerar sökväg. |
| `autoset()` | Provar spänningsområden och tidbaser tills signalen fyller skärmen rimligt — det som "AutoSetup"-knappen gör. |

**Resurs:** `picoscope://state` — aktuell konfiguration och senaste fångst som
läsbar resurs.

---

## 5. Genomförande — steg för steg

**Steg 0 — Förarbete (hårdvara)** ✅ 2026-09-12
- Installera PicoSDK 64-bit. Verifiera att `ps2000.dll` finns.
- Koppla in PS2104, kör Picos egen PicoScope-app och bekräfta att den ser enheten.
- Kör ett minimalt Python-skript som öppnar enheten och skriver ut
  `ps2000_get_unit_info()`. **Detta är grindvakten** — går inte det här, är
  resten meningslöst.

**Steg 1 — Skelett** ✅ 2026-09-12
- `pyproject.toml`, paketstruktur, `mcp`-beroendet (FastMCP).
- Server som startar, exponerar `list_devices` mot mock-backenden.
- Verifiera att Claude Code ser servern via `.mcp.json`.

**Steg 2 — Mock-backend + analys** ✅ 2026-09-12
- Signalgenerator i mocken: sinus, fyrkant, ramp, brus, valbar frekvens/amplitud.
- `analysis.py` med Vpp/RMS/frekvens. Frekvens via nollgenomgångar med hysteres,
  inte via FFT-topp — robustare för fyrkant och låga frekvenser.
- Enhetstester som mäter mockens kända signaler och jämför mot facit.

**Steg 3 — Riktig ps2000-backend** ✅ 2026-09-12 (trigg-grenen kvar att prova)
- `open_unit`, `set_channel`, `set_trigger`, `get_timebase`, `run_block`,
  `ready`-polling, `get_values`.
- ADC-räknare → volt via `max_adc`-skalning per spänningsområde.
- Tidbasval: välj snabbaste tidbas som täcker begärd `duration_s` med begärt
  antal sampel; rapportera faktisk samplingshastighet tillbaka (den blir sällan
  exakt den man bad om).

**Steg 4 — Export och presentation** ✅ 2026-09-12
- CSV (tid, spänning), NPZ för vidare analys, PNG via matplotlib.
- Nedsampling för svaret: min/max-decimering, inte var N:te punkt — annars
  försvinner spikar.

**Steg 5 — Robusthet** ✅ 2026-09-12 (timeout-vägen oprövad mot hårdvara)
- Tydliga fel: enhet upptagen, USB frånkopplad mitt i fångst, överstyrning
  (signal klipper mot områdesgränsen → föreslå större område).
- Timeout på trigg som aldrig löser ut.
- Städa upp USB-handtaget vid avstängning.

**Steg 6b — Live-display** ✅ 2026-09-12 *(tillkom under byggandet, fanns inte i
den ursprungliga planen)*
- Lokal HTTP-server i serverprocessen, sida på `127.0.0.1:8071`, öppnas i ett
  Edge-fönster i app-läge vid **första** verktygsanropet.
- Kroken sitter i `tool()`-dekoratorn — den enda punkt varje anrop passerar.
- Visar kurvan på rutnät med V/div och ms/div, mätvärden, kanal/trigg,
  versionsbanner och en logg över MCP-anrop. Uppdateras var 400:e ms.
- Simulerade fångster märks **SIMULERAD** i orange, så en mock aldrig kan
  misstas för en mätning.
- `PICOSCOPE_UI=0` stänger av (testerna sätter det), `PICOSCOPE_UI_PORT` flyttar.

**Steg 6 — Dokumentation** ✅ 2026-09-12
- README med installation av PicoSDK, `.mcp.json`-exempel, exempeldialog.

---

## 6. Risker

| Risk | Hantering |
|---|---|
| Fel drivrutinsfamilj (`ps2000a` istället för `ps2000`) | ~~Steg 0 verifierar~~ — verifierat 2026-09-12: `ps2000` svarar, variant "2104". |
| PicoSDK saknas på maskinen | ~~Mock-backend gör utveckling möjlig ändå~~ — löst 2026-09-12: PicoScope 7-appen bär `ps2000.dll`, och `_ensure_dll_on_path()` hittar den. |
| 32/64-bitars DLL-krock med Python | Använd 64-bitars PicoSDK till 64-bitars Python. Kontrolleras i steg 0. |
| Stora dataset spränger LLM-kontexten | Verktyg returnerar aldrig råa arrayer — princip 3 ovan. |
| Drivrutinen är inte trådsäker | En session, serialiserade anrop. |

---

## 7. Definition of done (v1)

- [x] `open_device` hittar och öppnar en riktig PS2104. *(2026-09-12, variant 2104 serienr <serial>)*
- [ ] `capture_block` på en känd signal (t.ex. 1 kHz fyrkant från en funktionsgenerator eller ett Arduino-PWM) ger rätt frekvens ±1 %.
- [x] `export_capture` producerar en PNG som ser rätt ut för ögat. *(mock, 2 kHz fyrkant 30 % duty — verifierad 2026-09-12; kvarstår mot riktig signal)*
- [x] Hela verktygsuppsättningen fungerar mot mock-backenden utan hårdvara. *(18 enhetstester + stdio-röktest, 2026-09-12)*
- [x] README räcker för att sätta upp servern på en ny dator. *(2026-09-12)*
- [ ] Taggad `v0.01` enligt det globala versionsregelverket.

---

## 7b. Miljövariabler

| Variabel | Default | Effekt |
|---|---|---|
| `CAPTURE_DIR` | `./captures` | Var exporterade filer hamnar. |
| `PICOSCOPE_UI` | `1` | `0` stänger av display och Edge-fönster. |
| `PICOSCOPE_UI_PORT` | `8071` | Startport; tio portar provas uppåt. |
| `PICOSDK_DIR` | *(auto)* | Katalog med `ps2000.dll` när autosökningen missar. |

---

## 8. Öppna frågor

1. Ska servern köras via `stdio` (lokalt, enklast) eller även kunna exponeras
   över nätverket så scopet kan sitta på en annan dator? **Beslutat 2026-09-12: stdio i v1.**
2. Behövs kontinuerlig streaming i v1, eller räcker blockfångst?
   **Beslutat 2026-09-12: blockfångst i v1, streaming i v2.**
3. Var ska exporterade filer hamna? **Beslutat 2026-09-12: `./captures/` i projektroten,
   konfigurerbart via `CAPTURE_DIR`.**
