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
mcp_picoscope/backends/mock.py   Simulerad signalkälla — facit för testerna
mcp_picoscope/backends/ps2000.py Riktig hårdvara via ps2000.dll. OPRÖVAD, se nedan.
tests/test_analysis.py           Mätningar mot mockens kända signaler
tests/test_stdio.py              Röktest över riktig stdio-transport
```

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

**Steg 0 är inte gjort.** PicoSDK är inte installerat på utvecklingsdatorn, och scopet
syns i Windows som `VID_0CE9&PID_1007` med `Status: Error` — uppräknat men utan
drivrutin. Hela `backends/ps2000.py` är därför skriven men **aldrig körd**.
Ändra den gärna, men påstå inte att den fungerar.

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
PicoScope PS2104: 1 kanal, 8 bitar, ingen signalgenerator. PicoSDK måste vara
64-bitars för att matcha Pythonen.
