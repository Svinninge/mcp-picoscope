# TODO — mcp-picoscope

Aktiv backlog. Handhållen i den här repon (till skillnad från ett tidigare projekt, där
TODO.md genereras ur GitHub Issues). Avklarat flyttas ner under **Klart** med
datum och en rad om vad som faktiskt gjordes.

Prioritet: 🔴 blockerande · 🟡 nästa · 🟢 när tillfälle ges

---

## Hårdvara — kvar att verifiera

- 🔴 **Definition of done: frekvens ±1 % på känd signal.** Kvar. Kräver en
  periodisk källa — ett Arduino-PWM, en funktionsgenerator, eller nätbrummet
  (50,00 Hz, nätet regleras hårdare än vår ±1 %-gräns) med sonden som antenn.
- 🟡 **Triggvägen är oprövad mot hårdvara.** `configure_trigger(mode="edge")`
  och timeout-grenen i `_wait_ready` är körd som logik, aldrig mot en enhet som
  faktiskt väntar på en flank. Kräver också en signal.
- 🟢 **Buffertdjupet är 8092 sampel**, inte 32768. `capture_block` klampar redan,
  men en begäran om fler sampel svarar tyst med färre — den borde säga det.

## Nästa

- 🟡 **Kurvan i MCP-svaret är onödigt dyr.** Mätt 2026-09-12 genom den
  registrerade servern: ett `autoset`-svar bar 200 punkter som nästlade listor
  med full flyttalsprecision (`0.002761925107577746`) — flera tusen tokens för
  en kurva som ska läsas av en LLM. Vi kapar antalet punkter men inte antalet
  siffror. Runda till ~5 signifikanta siffror och överväg färre punkter i
  verktygssvaret (displayen har ändå sina 1600). Det här går rakt emot
  projektets egen princip om att aldrig spränga kontextfönstret.

- 🔴 **Brus rapporteras som en frekvens.** Sett live 2026-09-12: med okopplad
  sond valde `autoset` ±0,5 V, och 30 mV brus blev "456 Hz" — över
  `MIN_SWING_FRAC` (2 % av området), alltså släpptes det igenom som en signal.
  Amplitudtröskeln ensam räcker inte; **periodiciteten** måste också vägas in.
  `period_jitter_pct` räknas redan ut — brus ger tiotals procent jitter, en
  riktig signal någon tiondel. Vägra rapportera frekvens över en jittergräns.
  Ett instrument som hittar på en siffra är värre än ett som säger "vet ej".

- 🟢 **`hardware_present()` läser "upptagen" som "saknas".** Hårdvarutestet
  hoppas över när enheten redan är öppen av en annan session — sant men
  missvisande; det borde säga vilket.
- 🟢 **Displayen visar bara senaste fångsten.** Fångstlistan finns i state men
  ritas inte — en klickbar historik vore billig.

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
- **2026-09-12 — Displayen skalbar, och ett fönster i stället för flera.**
  Zoomknappar (40–200 %, sparas i webbläsaren) plus brytpunkter som fäller ihop
  layouten ned till ~320×260. Fönsterstarten styrs nu av om sidan pollat de
  senaste 6 sekunderna, inte av en process-flagga — mätt 67 anrop → 1 fönster,
  och ett stängt fönster kommer tillbaka vid nästa anrop. Två buggar på vägen:
  UI-servern kunde **kapa** en annan sessions port på Windows
  (`allow_reuse_address`), och kurvan ritades fel under zoom eftersom
  `getBoundingClientRect()` och `clientHeight` inte mäter samma sak.
- **2026-09-12 — Live-display i Edge.** `ui.py` + `ui.html`: lokal server på
  8071, kurva med rutnät och V/div, mätvärden, kanal/trigg och en logg över
  MCP-anrop. Öppnas automatiskt i ett Edge-fönster vid första verktygsanropet
  (en gång per process), `PICOSCOPE_UI=0` stänger av. Verifierad mot en
  1 kHz-mocksignal: 1,0002 kHz och 50,0 % duty på skärmen.
- **2026-09-12 — Nollpunkten verifierad.** Kortsluten ingång, alla åtta
  områden: värsta offset 0,14 LSB, alltså under upplösningen. Brusgolvet på
  ±0,1 V är 0,99 mV Vpp ≈ 1,3 LSB. Skript: `tools/verify_zero.py`.
- **2026-09-12 — Voltskalan verifierad.** `MAX_ADC = 32767` mätt mot ett
  1,5 V alkaliskt AA: ±2/5/10/20 V läste 1,6007 / 1,6120 / 1,5931 / 1,6399 V —
  överens inom 47 mV, och absolutvärdet inom 7 % av cellens nominella.
  Skript: `tools/verify_volt_scale.py`.
- **2026-09-12 — Arbetsregler ärvda från ett tidigare projekt.** SOUL.md, CLAUDE.md,
  LESSONS.md och TODO.md anpassade för ett hårdvarunära MCP-projekt.
