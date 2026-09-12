# LESSONS — mcp-picoscope

Lärdomar från misstag och korrigeringar, nyast överst. Läs vid sessionsstart
efter [SOUL.md](SOUL.md). Vid beslut som påminner om en lärdom: citera regeln
explicit (`"Per LESSONS YYYY-MM-DD: ..."`).

Format: en rubrik med datum, 1–2 meningar med regeln och kontexten som gav den.

---

## 2026-09-12 — Ett fönster överlever sin server, och sidan kan inte stänga sig själv

Sju displayfönster stod öppna: varje testsession öppnade ett, och att stoppa
processen dödade servern men lämnade fönstret kvar med en frusen mätning. Första
fixen — `window.close()` i sidan — såg ut att fungera i ett prov där hela
processträdet dödades, men **isolerat mätt vägrar Chromium** stänga ett fönster
som skriptet inte öppnat. Den som öppnar ett fönster är den enda som kan stänga
det: kör displayen i en **egen Edge-profil** så att dess processer går att
identifiera och avsluta utan att röra användarens egen webbläsare. **Mät alltid
en fix isolerat** — det första provet bevisade fel sak.

## 2026-09-12 — Skriv aldrig Windows-sökvägar med backslash-escape i ett heredoc-skript

`"tools\verify_zero.py"` i en icke-rå Python-sträng blev `tools` + vertikaltab +
`erify_zero.py` i README — ett dokumenterat kommando som inte gick att köra. Värre
var att **rättningen misslyckades tyst** flera gånger: strängen jag sökte med bar
samma escape-problem, så `replace()` matchade inget medan `in` såg ut att hitta.
Bygg sådana rader ur `chr()`/råsträngar, eller skriv om hela raden i stället för
att patcha den, och **läs alltid tillbaka filen** för att se att ändringen tog.

## 2026-09-12 — Ett hårdvarutest som får falla tillbaka på mocken testar ingenting

`open_device(backend="auto")` faller tillbaka på mocken när hårdvaran saknas —
bekvämt i drift, värdelöst i ett test. `tests/test_hardware.py` begär därför
`backend="ps2000"` explicit och verifierar att variant-strängen är "2104".
Första körningen fällde direkt ett `AttributeError`: `_read_info` anropade ett
`_timebase_limits` som aldrig skrivits. Med auto-fallback hade testet lyst grönt.

## 2026-09-12 — `find_library` söker i PATH, och ingen lägger Pico där

`picosdk` laddar DLL:en via `ctypes.util.find_library`, som på Windows söker
`PATH`. Varken PicoSDK-installationen eller PicoScope-appen lägger sin katalog
där, så importen misslyckas på en maskin där drivrutinen är installerad och
fungerar. Leta upp katalogen i koden i stället för att sätta `PATH` för hand i
ett skal — handpåläggningen följer inte med till nästa session.

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
