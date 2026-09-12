# mcp-picoscope

MCP-server som låter Claude styra och läsa av ett **PicoScope PS2104**
USB-oscilloskop.

> **Status: planering.** Ingen kod är skriven än — se [PLAN.md](PLAN.md) för
> arkitektur, verktygslista och genomförandeplan.

## Tanken

Exponera oscilloskopet som MCP-verktyg, så att man kan säga:

- "Anslut till picoscopet och visa vad som ligger på kanal A"
- "Trigga på stigande flank vid 1,5 V och fånga 10 ms"
- "Vad är frekvensen och Vpp på signalen?"

## Förutsättningar

| Krav | Not |
|---|---|
| PicoScope PS2104 | 1 kanal, 8 bitar, legacy `ps2000`-drivrutin (**inte** `ps2000a`) |
| PicoSDK 64-bit | Installeras separat från Pico Technology — innehåller `ps2000.dll` |
| Python 3.11+ | 64-bitars, måste matcha SDK:ns bitness |
| `picosdk` | Picos officiella Python-wrappers |

En **mock-backend** ingår i planen, så servern går att utveckla och testa helt
utan hårdvara.

## Kom igång

Ännu inte implementerat. Följ stegen i [PLAN.md](PLAN.md) — steg 0 är att
verifiera att PicoSDK ser enheten innan något annat byggs.
