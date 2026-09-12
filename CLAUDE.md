# mcp-picoscope — AI-instruktioner (entrypoint)

Tunn entrypoint. MCP-server som exponerar ett **PicoScope PS2104** som verktyg åt
en Claude-session: öppna enheten, ställ kanal och trigg, fånga ett block, mät och
exportera.

**Projektets kärnprincip:** servern **mäter och rapporterar**. Den matar aldrig ut
signal och rör aldrig mätobjektet. Den svarar aldrig med råa sampel — statistik,
nedsamplad kurva och filsökväg, annars spränger en fångst kontextfönstret.

## Bas-kontext (läs vid sessionsstart)

- **[SOUL.md](SOUL.md)** — HUR vi arbetar (godkännanden, plan-läge, mini-sprint,
  elegans-paus, lessons-loop, hårdvaru-, kod- och git-regler). **Auktoritativ.**
- **[LESSONS.md](LESSONS.md)** — lärdomar från tidigare misstag. Läs efter SOUL.

## Läs on-demand

- **[PLAN.md](PLAN.md)** — mål, arkitektur, steg 0–6, risker, öppna frågor.
  Läs när uppgiften berör arkitektur eller vad som ska byggas härnäst.
  **Observera:** PLAN.md beskriver måldesignen. Koden är facit — `capture_streaming`
  finns i planens verktygstabell men är inte byggd (v2, se TODO.md).
- **[TODO.md](TODO.md)** — aktiv backlog, handhållen i den här repon.
- **[README.md](README.md)** — installation och kom-igång.

## Layout

```
mcp_picoscope/server.py          MCP-ytan. Tunn: översätter, räknar inte.
mcp_picoscope/scope.py           Värdetyper, backend-protokoll, ScopeSession (låset)
mcp_picoscope/analysis.py        Vpp/RMS/frekvens/duty + min/max-decimering
mcp_picoscope/export.py          CSV / NPZ / PNG under captures/
mcp_picoscope/ui.py              Lokal webbserver + Edge-start (port 8071)
mcp_picoscope/ui.html            Sidan: kurva, mätvärden, MCP-aktivitet
mcp_picoscope/backends/mock.py   Simulerad signalkälla — facit för testerna
mcp_picoscope/backends/ps2000.py Riktig hårdvara via ps2000.dll. Verifierad mot PS2104.
tests/test_analysis.py           Mätningar mot mockens kända signaler
tests/test_stdio.py              Röktest över riktig stdio-transport (mock)
tests/test_hardware.py           Röktest mot riktigt scope; hoppas över utan enhet
tools/step0_verify.py            Hårdvaruidentitet: variant, områden, timebaser
tools/verify_volt_scale.py       Skalan mot känd spänning (MAX_ADC)
tools/verify_zero.py             Offset, kortsluten ingång
tools/ui_session.py              Håller en session öppen så displayen lever
```

**Versioner:** `SYSTEM_VERSION` i `mcp_picoscope/__init__.py` speglar senaste
git-tagg, `deploy_version.txt` deployen. Båda visas via `version_line()` — i
`get_server_info()` och i displayens huvud, enligt det globala regelverket.

## Kör och testa

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q          # allt
.\.venv\Scripts\python.exe tests\test_stdio.py         # röktest, startar servern
.\.venv\Scripts\python.exe -m mcp_picoscope.server     # servern manuellt (väntar på stdio)
```

Servern registreras för Claude Code via [.mcp.json](.mcp.json) i projektroten.

## Fallgropar — läs innan du ändrar

**PS2104 är `ps2000`, inte `ps2000a`.** Fel API-familj svarar "unit not found",
vilket ser ut som trasig hårdvara. Detta är projektets enskilt viktigaste
tekniska faktum.

**Steg 0 är gjort (2026-09-12).** Enheten svarar: variant `2104`, serienr
`<serial>`, hårdvara 4, drivrutin 3.0.152.6217. Uppmätt, inte antaget:
spänningsområden **100 mV–20 V** (20 mV och 50 mV avvisas), timebase 0–19
(20 ns–10,49 ms), **buffertdjup 8092 sampel**. Kalibreringen är verifierad i båda ändar: **skalan** mot en 1,5 V-cell
(fyra områden inom 47 mV, `MAX_ADC = 32767` bekräftad) och **nollan** mot
kortsluten ingång (värsta offset 0,14 LSB). Kvar: **frekvens ±1 % mot känd
signal** och **flanktriggen** — se TODO.md.

**Drivrutinen hittas inte av sig själv.** `picosdk` löser DLL:en med
`ctypes.util.find_library`, som på Windows söker i `PATH` — och ingenting lägger
Picos katalog där. `_ensure_dll_on_path()` i `backends/ps2000.py` gör det, med
`PICOSDK_DIR` som övertrumfar. På den här maskinen finns `ps2000.dll` inte i
`SDK\lib` utan i `PicoScope 7 T&M Stable\`, eftersom appen installerades i
stället för SDK:n; båda fungerar.

**Ett fönster på hela maskinen — inte per process.** Kroken sitter i
`tool()`-dekoratorn eftersom varje verktygsanrop redan passerar den. Att sidan
**pollat nyligen** är beviset på att ett fönster tittar (`viewer_present()`,
6 s), och samma bevis skrivs till `%TEMP%\mcp-picoscope-ui.json` som varje
serverprocess läser (`window_claim()`). Det räckte inte med en variabel i
processen: två serverprocesser — Claude Codes registrerade server och en egen
`tools/ui_session.py` — öppnade varsitt fönster, och det finns **ett** PS2104.
Beslutet ligger i `should_launch()` just för att gå att testa utan webbläsare
(`tests/test_ui.py`). `PICOSCOPE_UI_BROWSER=0` serverar sidan utan att öppna
något.

**Displayen minns zoom, position och storlek** i samma fil, inte i
`localStorage` — den är per origin, och porten byts så fort en annan process
redan äger 8071. Ramoffseten (skillnaden mellan var vi bad Edge placera
fönstret och var innehållet hamnade) mäts upp vid första rapporten efter en
start; utan den vandrar fönstret en titelrad nedåt varje gång.
`PICOSCOPE_UI=0` stänger av alltihop; testerna sätter det, och allt som körs
obevakat bör göra detsamma. Sidan **läser** sessionen och kan aldrig styra
hårdvaran — ett UI som också kunde trycka på knappar hade behövt sessionslåset
och en behörighetsfråga.

**Två servrar kan kapa samma port på Windows.** `HTTPServer` sätter
`allow_reuse_address`, och `SO_REUSEADDR` betyder inte samma sak på Windows som
på Unix: där får en ny socket **ta över** en levande lyssnare. Två sessioner
"ägde" 8071 samtidigt och anslutningarna landade på den som vann kapplöpningen,
så fönstret visade en annan sessions scope. `_Server.allow_reuse_address = False`
gör att bindningen misslyckas ärligt och portskanningen går vidare till 8072.

**CSS-zoom: mät på ett ställe.** `getBoundingClientRect()` är zoom-skalad,
`clientWidth`/`clientHeight` är det inte. Att mäta canvasen med den ena och rita
med den andra sträcker kurvan och flyttar nollinjen från mitten. `fit()` mäter
en gång till `viewW`/`viewH`, `draw()` använder bara dem. Av samma skäl sätts
body-höjden i skript: `100vh` räknas *före* zoomen, så vid 70 % blir sidan 1/0,7
gånger för hög.

**En `<canvas>` i en flexkolumn växer av sig själv.** Att skriva `canvas.height`
sätter elementets *intrinsic* storlek, så en `flex: 1`-canvas trycker ut resten
av kolumnen vid varje omritning. Därför ligger den `position: absolute` i en
wrapper med `min-height: 0` och får sin storlek därifrån. Buggen syntes som att
mätvärdesraden "försvann" och kurvan var avklippt nedtill.

**Mocken imiterar hårdvara med flit.** Sampelintervallet snäpper till en
2^n-timebase, sampel kvantiseras till 8 bitar av området, och en för stor signal
klipper. Det är inte krångel — det tvingar varje anropare att läsa **faktisk**
samplingshastighet ur fångsten istället för att lita på den den bad om.

**Frekvens mäts på nollgenomgångar, inte FFT.** En fyrkant lägger det mesta av
energin i övertonerna och en långsam signal hinner inte två perioder i fönstret.
Genomgångarna klarar båda och ger duty cycle på köpet. Nivån är **mittpunkten
mellan min och max**, inte medelvärdet: en 20 %-fyrkant har ett medelvärde långt
från sin egen mittpunkt, och mätt mot det blir varje sådan våg ~50 %.

**Nedsampling är min/max per hink.** Var N:te sampel tappar spikarna, vilket är
precis det man köpte ett oscilloskop för att se. `test_downsample_keeps_the_spike`
fäller bygget om någon förenklar det.

**Ett undantag som når MCP-ytan måste bli `ToolError`.** Dekoratorn `tool()` i
`server.py` gör det. Allt annat blir "Error executing tool X" i sessionen, vilket
inte hjälper någon som inte kan se skärmen.

**`mcp` 2.x, inte 1.x.** `FastMCP` heter `MCPServer` sedan 2.0
(`from mcp.server.mcpserver import MCPServer`). Egen venv — installera inte i den
globala Pythonen, den bär platformio.

## Hårdvara

Byggd mot `dev-laptop` (Dell XPS 15 9500, Windows 11, Python 3.13 64-bit).
PicoScope PS2104 (serienr <serial>, kalibrerad <date>): 1 kanal, 8 bitar,
ingen signalgenerator, 50 MS/s, 8092 sampels buffert, 100 mV–20 V.
Drivrutinen kom med **PicoScope 7 T&M** via winget
(`PicoTechnology.Picoscope.T&M`) — PicoSDK som separat paket behövs alltså inte,
appen bär samma `ps2000.dll`. 64-bitars för att matcha Pythonen.
