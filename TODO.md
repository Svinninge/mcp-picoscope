# TODO — mcp-picoscope

Aktiv backlog. Handhållen i den här repon (till skillnad från ett tidigare projekt, där
TODO.md genereras ur GitHub Issues). Avklarat flyttas ner under **Klart** med
datum och en rad om vad som faktiskt gjordes.

Prioritet: 🔴 blockerande · 🟡 nästa · 🟢 när tillfälle ges

---

## Blockerat på hårdvara

- 🔴 **Steg 0 — installera PicoSDK 64-bit.** Scopet syns i Windows som
  `VID_0CE9&PID_1007` med `Status: Error`: uppräknat, men utan drivrutin.
  Per laddar ner och kör installationen (SOUL.md: agenten installerar inte
  systemprogramvara). Verifiera därefter att `ps2000.dll` finns och att Picos
  egen PicoScope-app ser enheten.
- 🔴 **Verifiera `backends/ps2000.py` mot riktig enhet.** Filen är skriven men
  aldrig körd. Kontrollera i tur och ordning: `open_unit` ger handtag > 0,
  `get_unit_info` rapporterar variant "2104", `_probe_ranges` ger enhetens
  faktiska spänningsområden, `get_timebase` ger rimliga intervall, en fångst på
  känd signal ger rätt frekvens ±1 %.
- 🔴 **Mät `MAX_ADC` istället för att anta 32767.** Den legacy-drivrutinen
  skalar till int16, men det är antaget, inte verifierat. En felskalning ger
  rätt frekvens och fel volt — den sortens fel syns inte på kurvan.
- 🟡 **Känd signal att mäta mot.** Ett Arduino-PWM eller en funktionsgenerator
  med känd frekvens, så att definition-of-done går att bevisa och inte bedöma.

## Nästa

- 🟡 **`capture_streaming(duration_s, rate)`** — finns i PLAN.md §4 men är inte
  byggd; planens §8 föreslår block i v1 och streaming i v2. Skriv den när
  blockvägen är verifierad mot hårdvara.
- 🟡 **Timeout-vägen i `_wait_ready` är oprövad.** Testad logik, otestad mot en
  trigg som faktiskt aldrig löser ut.
- 🟢 **`autoset` kollar inte om signalen är för liten för det valda området.**
  Den väljer minsta område som rymmer topparna, men en signal under ett par
  procent av området rapporteras bara som "ingen periodisk signal". Den borde
  säga "signalen är under brusgolvet på detta område".
- 🟢 **AC-koppling i mocken är medelvärdesavdrag**, inte ett högpassfilter med
  brytfrekvens. Skillnaden syns på låga frekvenser.

## Öppna frågor (ur PLAN.md §8)

- Stdio räcker i v1 — nätverksexponering först om scopet ska sitta på en annan
  dator. **Beslutat: stdio.**
- Exportkatalog: `./captures/`, konfigurerbar via `CAPTURE_DIR`. **Beslutat.**

---

## Klart

- **2026-09-12 — Steg 1–2 och 4–5 byggda mot mock-backend.** Paketstruktur,
  `ScopeSession` med lås, mock-backend som imiterar timebase-snäppning,
  8-bitarskvantisering och klippning, `analysis.py` (nollgenomgångar med
  hysteres, mittpunkt som nivå), `export.py` (CSV/NPZ/PNG), tolv MCP-verktyg
  och resursen `picoscope://state`. 18 enhetstester mot mockens facit + röktest
  över riktig stdio-transport, allt grönt. `.mcp.json` för Claude Code.
- **2026-09-12 — Steg 3 skriven men oprövad.** `backends/ps2000.py` mot det
  legacy `ps2000`-API:t: open/close, kanal, trigg, timebase-val, blockfångst,
  ADC→volt. Väntar på PicoSDK.
- **2026-09-12 — Arbetsregler ärvda från ett tidigare projekt.** SOUL.md, CLAUDE.md,
  LESSONS.md och TODO.md anpassade för ett hårdvarunära MCP-projekt.
