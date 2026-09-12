# TODO — mcp-picoscope

Aktiv backlog. Handhållen i den här repon (till skillnad från ett tidigare projekt, där
TODO.md genereras ur GitHub Issues). Avklarat flyttas ner under **Klart** med
datum och en rad om vad som faktiskt gjordes.

Prioritet: 🔴 blockerande · 🟡 nästa · 🟢 när tillfälle ges

---

## Hårdvara — kvar att verifiera

- 🔴 **Voltskalan mot en känd spänning.** `MAX_ADC = 32767` är `picosdk`:s egen
  konvention för `ps2000` (wrappern har ingen `maximum_value` att fråga och
  faller tillbaka på `2**15-1`), men den är inte mätt. Ett fel här ger **rätt
  frekvens och fel volt** — det syns inte på kurvan. Koppla något känt: ett
  AA-batteri (~1,5 V DC) räcker för skalan, ett Arduino-PWM ger både skala och
  frekvens.
- 🔴 **Definition of done: frekvens ±1 % på känd signal.** Samma mätning stänger
  både denna och punkten ovan.
- 🟡 **Triggvägen är oprövad mot hårdvara.** `configure_trigger(mode="edge")`
  och timeout-grenen i `_wait_ready` är körd som logik, aldrig mot en enhet som
  faktiskt väntar på en flank. Kräver också en signal.
- 🟢 **Buffertdjupet är 8092 sampel**, inte 32768. `capture_block` klampar redan,
  men en begäran om fler sampel svarar tyst med färre — den borde säga det.

## Nästa

- 🟡 **`capture_streaming(duration_s, rate)`** — finns i PLAN.md §4 men är inte
  byggd; planens §8 föreslår block i v1 och streaming i v2. Skriv den när
  blockvägen är verifierad mot hårdvara.
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
- **2026-09-12 — Steg 0 PASSERAD.** PicoScope 7 T&M installerad via winget
  (bär `ps2000.dll`; separat PicoSDK behövdes inte). Enheten gick från
  `Status: Error` till `OK` och svarar: variant 2104, serienr <serial>,
  hårdvara 4, drivrutin 3.0.152.6217, kalibrerad <date>. Uppmätt: områden
  100 mV–20 V (20/50 mV avvisas), timebase 0–19 = 20 ns–10,49 ms, 8092 sampels
  buffert.
- **2026-09-12 — Steg 3 verifierad mot riktig enhet.** `backends/ps2000.py`
  öppnar, konfigurerar, fångar och exporterar genom MCP-servern.
  `_ensure_dll_on_path()` tillagd — `picosdk` hittar annars inte drivrutinen.
  `tests/test_hardware.py` (som vägrar mock-fallback) fällde ett saknat
  `_timebase_limits`; rättat.
- **2026-09-12 — Arbetsregler ärvda från ett tidigare projekt.** SOUL.md, CLAUDE.md,
  LESSONS.md och TODO.md anpassade för ett hårdvarunära MCP-projekt.
