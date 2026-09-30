# DISTRIBUTION_PLAN.md - how Cygnus reaches students, and everyone else

Status: decided 2026-09-30, not started past the provider setting.

## Decision

- **Students: free.** Mostly 18 and over. They install Cygnus on their own
  Windows PC and bring their own model key (`CYGNUS_PROVIDER`,
  `CYGNUS_API_KEY`, docs/API_KEY.md). No server of ours is involved.
- **Everyone else: paid.** The same installed program, plus a small cloud
  service of ours for accounts, the model keys, metering and the phone.

Cygnus's job is acting on the PC it runs on: opening apps, reading windows,
typing into them. A cloud service cannot do that, so the installed program is
the product in both cases; the cloud only ever sits beside it.

## Students (free)

### Which model

| Student | Provider | Why |
|---|---|---|
| 18 and over | `gemini` (Google AI Studio free tier) | Free, no card; gemini-3.6-flash, 3-flash-preview and 2.5-flash called Cygnus's tools right in 1.6-2.8 s (config.py, 2026-09-26) |
| Under 18 | `ollama`, or `openai` with a parent's permission | Gemini API and OpenRouter terms require 18+ (OpenRouter since 2026-08-31); OpenAI allows 13+ with permission, but is paid |
| No internet, or nothing may leave the PC | `ollama` | Local; slow on a laptop CPU |

The free tier's limit is requests a day, not quality: gemini-3.6-flash
refused at 20 requests a day (429, 2026-09-30), and a turn makes 2-4. Seven
Gemini models in turn give very roughly 35-70 turns a day per student - not
yet measured across all seven. **One key per student**: the quota is per
Google project, so a shared class key runs dry in minutes.

Not a default until measured: Groq's llama-3.3-70b allows 12k tokens a minute
on the free tier, and one Cygnus call is about 7.5k tokens (prompt and 32
tools, 2026-09-12), so a three-call turn waits on the limit.

**Measured 2026-09-30** (`scripts/bench/providers.py`: the agent loop's real
first call - its system prompt and 16 tools, about 1.4-1.9k prompt tokens -
for 8 requests whose right first tool is known; one round, on the author's
laptop):

| Model | Right | Refused | p50 | Slowest |
|---|---|---|---|---|
| gemini-3.1-flash-lite | 8/8 | 0 | 2.7 s | 4.4 s |
| gemini-3.5-flash | 6/8 | 2 (503, busy) | 3.2 s | 6.2 s |
| gemini-2.5-flash | 3/8 | 4 (429) | 1.7 s | 1.9 s |
| gemini-3-flash-preview | 2/8 | 6 (429) | 1.8 s | 2.0 s |
| ollama jarvis-qwen3 (4b, 8k window) | 7/8 | 0 | 3.0 s | 31.8 s (cold) |
| ollama qwen3:4b-instruct | 6/8 | 0 | 2.9 s | 33.3 s (cold) |
| ollama qwen3:1.7b | 4/8 | 0 | 6.7 s | 20.3 s |

What it shows: every Gemini model that answered chose the right tool but one
(2.5-flash said "noted" to "remember that my exam is on Friday"), so the free
tier's problem is **availability, not quality** - a per-minute limit of 5 calls
per model (seen on 2.5-flash), a daily one of about 20 (seen on 3.6-flash), and
503s when Google is busy. The 429s above are most likely the daily limit, spent
by the day's earlier tests; the bench now records which limit it was. Locally,
4B is the floor: 1.7B chose right half the time. The 8k-window build of
qwen3:4b beat the stock one, so the installer should build it
(`Modelfile.jarvis`) rather than pull the stock model alone.

**To do:** run it again on a fresh daily quota with `--rounds 3`, and on
Groq and OpenRouter with their keys, then put the table in docs/API_KEY.md.

### The local model follows the machine

To be measured before it ships - a starting point only:

| Machine | Local brain |
|---|---|
| Under 8 GB RAM | none: cloud only |
| 8-16 GB, no GPU | a 4B model, as the offline backup |
| 16 GB and up, or a GPU with 6 GB or more | a 4B-8B model, can be the main brain |

The tool schemas alone are ~3.7k tokens (config.py), so the local model needs
an 8k window, and 1-2B models are expected to fail at tool calls. Measured on
the author's laptop: qwen3:4b-instruct at 14 tok/s on the CPU, 28.9 s cold.

## The installable program

1. **One installer.** The Electron app with its own Python inside, so no
   Python, pip or npm on the student's PC. Signed, so SmartScreen does not
   stop it; updates itself from the releases. Whisper and the Playwright
   browser download on first run, with progress shown.
2. **First run.**
   1. Read the machine: RAM, cores, GPU and its memory, free disk.
   2. Choose the brain: a cloud provider (paste the key; a test call says it
      works or names the fix - `PROVIDER_PROBLEM`, `_why_no_model`), a local
      model offered only when the machine can run it, or - paid - sign in.
   3. Choose the Whisper model from the specs (tiny, base, small).
   4. Keep the key in Windows Credential Manager (`keyring`), not in `.env`.
   5. The first five minutes from docs/SETUP.md.
3. **The phone.** Today it has no login and needs Tailscale (PHONE_PLAN.md).
   Before it ships to students: pairing by QR code from the PC, a token per
   phone that can be revoked.

### Built so far (2026-09-30)

- `cygnus/setup/machine.py`: reads RAM, cores, graphics cards (their real
  memory, from the registry) and free disk, and recommends: no local model
  under ~8 GB, a local model only as the offline backup without a graphics
  card of 6 GB or more, and the Whisper model by RAM.
- `cygnus/setup/provider.py`: tries a key with one real tool call and says
  what to do in plain words (refused, quota used up but the key works, no
  such model, no tools, no connection); keeps the key in Windows Credential
  Manager and only the provider's name in `.env`. config.py reads the key
  from there when `.env` has none.
- `/setup/state`, `/setup/test`, `/setup/save` (`cygnus/server/setup_routes.py`):
  this computer only - the phone and anything proxied get 403 - and writes
  only as same-origin JSON.

Next: the setup page in the HUD that uses them, then the bundled installer.

## Paid (everyone else)

The installed program plus our gateway:

- accounts and a licence per seat or per school;
- our provider accounts behind it, so no one handles a key;
- metering and a spending cap per account;
- the phone relayed through it, instead of Tailscale.

What it costs us: every token, the service, and the duties of holding people's
conversations - the Data Privacy Act (RA 10173), a data processing agreement
with each provider, and a retention setting (OPEN_SOURCE_PLAN.md, Retention).

## Open

- **The licence.** "Free for students, paid for others" does not fit MIT,
  which lets anyone sell it. A source-available licence (PolyForm
  Noncommercial, the Business Source License) or a dual licence does. The
  owner's choice.
- How a student proves they are one for the free tier, if the paid tier and
  the free one share an installer.
- The age check: the free tier's docs say who may use which provider; nothing
  in Cygnus enforces it.

## Order

1. Now: students bring their own key (done: 00f10b1, deb16c4, 9f60e9f).
   Measure the student providers; add the age rule to docs/API_KEY.md.
2. The installer and first-run wizard, key storage, signing, updates.
3. Phone pairing.
4. The licence, then the gateway for the paid tier.
