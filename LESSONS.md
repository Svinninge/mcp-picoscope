# LESSONS — mcp-picoscope

Lärdomar från misstag och korrigeringar, nyast överst. Läs vid sessionsstart
efter [SOUL.md](SOUL.md). Vid beslut som påminner om en lärdom: citera regeln
explicit (`"Per LESSONS YYYY-MM-DD: ..."`).

Format: en rubrik med datum, 1–2 meningar med regeln och kontexten som gav den.

---

## 2026-09-12 — "Ansluten" betyder inte "tillgänglig"

PS2104:an satt i USB:n, men `Get-PnpDevice` visade `Status: Error` för
`VID_0CE9&PID_1007`: enheten är strömsatt och uppräknad, men utan drivrutin,
eftersom PicoSDK inte är installerat. **Kontrollera enhetens status i Windows
innan du drar slutsatser om DLL-anropet** — ett `unit not found` från `ps2000`
kan lika gärna betyda "ingen drivrutin" som "fel API-familj", och de två felen
har helt olika åtgärd.

## 2026-09-12 — `ps2000`, inte `ps2000a`

PS2104 tillhör 2000-seriens äldre generation och styrs av `ps2000.dll`.
`ps2000a`-familjen svarar `unit not found` på en PS2104 och felet ser ut som
trasig hårdvara. Slå upp modellen i Picos wrapper-repo innan du väljer
API-familj — gissa aldrig utifrån modellnumrets siffror.
