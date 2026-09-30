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

**Before publishing a recommendation:** run the tool-call probe
(`router_health`) and the latency and grounding benches against each student
provider, and put the measured table in docs/API_KEY.md.

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
