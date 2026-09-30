# Getting an API key for Cygnus

Cygnus needs a model to think with. You bring your own: pick one provider
below and make a key. About five minutes.

**The easy way:** start Cygnus and click **Set up** (or open
`http://127.0.0.1:8001/setup`). It shows what your PC can run, lets you pick
a provider, tries your key before keeping it, keeps the key in Windows
Credential Manager rather than a file, and restarts Cygnus with it.

**By hand:** put two lines in `.env`:

```ini
CYGNUS_PROVIDER=gemini
CYGNUS_API_KEY=paste-your-key-here
```

Then start Cygnus (or restart it: `.env` is read once, at start).

## Which provider?

| Provider | Costs | Card needed | Good for |
|---|---|---|---|
| **gemini** (Google AI Studio) | Free, with a daily limit | No | **Start here.** |
| **groq** | Free tier | No | Fast replies; a second free option |
| **openrouter** | Free models, and paid ones | No for free models | Trying many models |
| **openai** | Paid per use | Yes | If you already pay for it |
| **ollama** | Free, runs on your laptop | No key at all | No internet, or nothing may leave the machine |

**Under 18?** Google's Gemini API and OpenRouter require you to be 18 or
older. Use `ollama` (on your own laptop, no account), or `openai` with your
parent's or guardian's permission - OpenAI allows 13 and up with it.

**One key per person.** Free limits are per account: a key shared by a class
runs out in minutes, and everyone's use shows up on its owner's account.

Free tiers have limits per minute and per day, and providers change them
without notice. When Cygnus says "my model is out of quota", you have hit one:
wait for the reset it names, or switch provider.

## Gemini (Google AI Studio) - recommended

1. Open <https://aistudio.google.com/app/apikey> and sign in with a Google
   account.
2. Click **Create API key**. If it asks for a project, let it create one.
3. Copy the key. It starts with `AIza`.
4. In `.env`:
   ```ini
   CYGNUS_PROVIDER=gemini
   CYGNUS_API_KEY=AIza...
   ```

Cygnus tries several free Gemini models in turn, so when one reaches its daily
limit the next one answers. To use one model only, set `CYGNUS_MODEL`, for
example `CYGNUS_MODEL=gemini-2.5-flash`.

Your limits: the **Rate limit** page in AI Studio. Google may use what you send
on the free tier to improve its products; do not use it for anything private.

## Groq

1. Open <https://console.groq.com/keys> and sign in.
2. Click **Create API Key**, give it a name (say, `cygnus`), and submit.
3. Copy the key now - it is shown only once. It starts with `gsk_`.
4. In `.env`:
   ```ini
   CYGNUS_PROVIDER=groq
   CYGNUS_API_KEY=gsk_...
   ```

The default model is `llama-3.3-70b-versatile`; set `CYGNUS_MODEL` for another
one from the console's model list. Your limits: **Settings > Limits** in the
console.

## OpenRouter

1. Open <https://openrouter.ai/keys> and sign in.
2. Click **Create Key**, name it, and copy it. It starts with `sk-or-`.
3. Pick a model on <https://openrouter.ai/models>. Free ones end in `:free`.
   The free list changes, so Cygnus does not choose one for you.
4. In `.env`:
   ```ini
   CYGNUS_PROVIDER=openrouter
   CYGNUS_API_KEY=sk-or-...
   CYGNUS_MODEL=the-model-id:free
   ```

Free models allow few requests a day on an account that has never bought
credits. Pick one that supports **tools** (the model page says so): Cygnus
opens apps and sites by calling tools, and a model without them can only talk.

## OpenAI

OpenAI charges for every request, so **set a budget before you make a key**.

1. Open <https://platform.openai.com> and sign in. Add a payment method under
   **Billing**.
2. Under your project's **Limits**, set a monthly budget. Requests past it are
   refused, which is what you want.
3. Open **API keys**, click **Create new secret key**, and copy it. It starts
   with `sk-`.
4. In `.env`:
   ```ini
   CYGNUS_PROVIDER=openai
   CYGNUS_API_KEY=sk-...
   ```

The default model is `gpt-5.4-mini`, falling back to `gpt-5.4`; `CYGNUS_MODEL`
picks another.

## Ollama - no key

1. Install Ollama from <https://ollama.com>.
2. Download a model: `ollama pull qwen3:4b-instruct`
3. In `.env`:
   ```ini
   CYGNUS_PROVIDER=ollama
   ```

Slower than the cloud and weaker at multi-step tasks, but nothing you say goes
to a model provider. For no cloud at all, including the voice, also set
`JARVIS_LOCAL_ONLY=true` (see [PRIVACY.md](PRIVACY.md)).

## Any other endpoint

Any server that speaks the OpenAI chat API works:

```ini
CYGNUS_PROVIDER=custom
ROUTER_BASE_URL=https://your-endpoint/v1
CYGNUS_API_KEY=...          # if it needs one
CYGNUS_MODEL=the-model-name
```

## Keeping your key safe

An API key is a password that spends money or quota. Anyone who has it can use
your account.

- **Keep it only in `.env`.** That file is in `.gitignore`, so git never
  commits it. Never put the key in code, a screenshot, a chat, a shared
  document or an issue.
- **Never commit `.env`**, and never send it to someone "so they can run it
  too". Each person makes their own key.
- **One key per app.** Name it `cygnus` so you know what to revoke.
- **Leaked?** Delete the key on the provider's key page right away and make a
  new one. Deleting it from a file or a git commit is not enough: once it was
  pushed or shared, treat it as seen.
- **Paid providers:** set a spending limit on the account, so a runaway task
  cannot run up a bill.

## When it does not work

Cygnus says what is wrong, in the reply and on `http://127.0.0.1:8001/status`
(the `setup_problem` field):

| Cygnus says | Do this |
|---|---|
| "No API key is set for ..." | Add it in the setup (**Set up**), or put it in `.env` as `CYGNUS_API_KEY=...` and restart. |
| "The provider refused the API key" | The key is wrong, deleted, or copied with a space or a missing character. Copy it again, or make a new one. |
| "... is not a provider Cygnus knows" | Check the spelling of `CYGNUS_PROVIDER`: one of `gemini`, `groq`, `openrouter`, `openai`, `ollama`, `custom`. |
| "... needs CYGNUS_MODEL" | OpenRouter and custom endpoints need the model named. |
| "My model is out of quota" | A free-tier limit. Wait for the reset, or use another provider. |
| "I couldn't reach the model endpoint" | No internet, or `ROUTER_BASE_URL` is wrong (custom), or Ollama is not running. |
