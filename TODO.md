# TODO — mcp-picoscope

Aktiv backlog. Handhållen i den här repon (till skillnad från ett tidigare projekt, där
TODO.md genereras ur GitHub Issues). Avklarat flyttas ner under **Klart** med
datum och en rad om vad som faktiskt gjordes.

Prioritet: 🔴 blockerande · 🟡 nästa · 🟢 när tillfälle ges

---

## Hårdvara — kvar att verifiera

- 🟡 **Triggvägen är oprövad mot hårdvara.** `configure_trigger(mode="edge")`
  och timeout-grenen i `_wait_ready` är körd som logik, aldrig mot en enhet som
  faktiskt väntar på en flank. Kräver också en signal.
- 🟢 **Buffertdjupet är 8092 sampel**, inte 32768. `capture_block` klampar redan,
  men en begäran om fler sampel svarar tyst med färre — den borde säga det.

## Nästa

- 🟡 **Interaktiv trigg i displayen** — spårad i
  [issue #1](https://github.com/Svinninge/mcp-picoscope/issues/1). Klicka och
  dra triggnivån, single-svep, kontinuerlig omtriggning. Bryter principen att
  displayen läser och aldrig styr; frågorna om ägarskap, gränser och
  insamlingsloop står i issuen. Planfil krävs innan kod.

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
- **2026-09-12 — Definition of done: frekvens ±1 % uppfylld.** Funktionsgenerator,
  sinus 800 Hz, amplitud 3,0 V: uppmätt 799,37–800,20 Hz över fönster från 2 till
  200 ms, **0,03 %** fel på de längre och 0,09 % spridning. Vpp 3,02 V mot 3,0 V —
  inom ett ADC-steg (39 mV på ±5 V). `tools/measure_signal.py` gör om mätningen.
- **2026-09-12 — Brus rapporteras inte längre som en frekvens.** Uppmätt
  gräns i stället för gissad: riktiga vågformer (sinus, fyrkant, ramp, triangel,
  även sinus under 10 % brus) ligger på 0,06–0,71 % periodjitter; rent brus på
  58–73 % i mocken och 66–200 % på en okopplad PS2104-sond. Gränsen sattes till
  20 %, mitt i det tomma glappet. Fåcykelfallet — två flanker ger ett intervall
  och därmed 0 % jitter per definition, vilket gav "3756 Hz" på brus — fångas av
  formmåttet Vpp/stdev (2,0 fyrkant, 2,8 sinus, 3,5 ramp, 5–7 brus).
- **2026-09-12 — Fönstret överlever inte längre sin server.** Sju fönster hade
  hunnit samlas: varje testsession öppnade ett, och processen som dog lämnade
  det kvar med en frusen mätning. Displayen kör nu i en egen Edge-profil och
  `close_stale_windows()` stänger kvarglömda fönster — vid serveravslut och
  före varje start. `window.close()` i sidan räcker inte: Chromium vägrar för
  fönster som skriptet inte öppnat (uppmätt).
- **2026-09-12 — Ett fönster per maskin, och det minns var det stod.**
  Två serverprocesser öppnade varsitt fönster trots att det finns ett enda
  PS2104 — vakten var per process. Beviset att ett fönster tittar skrivs nu till
  `%TEMP%/mcp-picoscope-ui.json` som alla processer läser (`should_launch()`,
  testad i `tests/test_ui.py`). Samma fil bär zoom, position och storlek, med
  fönsterramen uppmätt så att fönstret inte vandrar nedåt för varje start.
  `PICOSCOPE_UI_BROWSER=0` serverar utan att öppna något.
- **2026-09-12 — Siffrorna avrundade i allt som lämnar `analysis.py`.**
  Statistik 6 signifikanta siffror, kurvpunkter 5. Ett svar med hårdvaruformade
  tal (`adc/32767`) gick från 7 593 till 4 716 tecken — **38 % mindre**, kurvan
  ensam 40 %. En 8-bitars ADC löser en del på 256; elva siffror var precision
  instrumentet inte har, betald i anroparens kontextfönster.
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
