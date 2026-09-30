"""provider.py - try a student's key before keeping it, then keep it safely.

test() makes one real call with the key - the same tool-call question
router_health asks - so the setup can say "it works" or name the fix before
anything is saved. save() puts the key in Windows Credential Manager and only
the provider's name in .env, so the key never sits in a plain file that a
backup, a screenshot or a zip of the folder would carry along.
"""

from __future__ import annotations

import re
import shutil
import time

KEYRING_SERVICE = "cygnus"


def endpoint(provider: str, model: str = "", base_url: str = "") -> tuple[str, str]:
    """(base URL, model to ask) for a provider, as config would fill them in."""
    from cygnus import config
    if provider == "gemini":
        return config.GOOGLE_AI_BASE_URL, model or config.GOOGLE_AI_MODELS[0]
    preset = config.PROVIDERS[provider]
    return base_url or preset.get("url", ""), model or preset.get("model", "")


def _plain(error: str) -> tuple[bool, str]:
    """(the key works, what to tell the student) for a failed call."""
    low = error.lower()
    if re.search(r"\b401\b|api.?key not valid|valid api key|invalid.{0,12}api.?key|incorrect api key", low):
        return False, "The provider refused this key. Copy it again, whole, or make a new one."
    if re.search(r"\b429\b|quota|rate limit|resource_exhausted", low):
        return True, "The key works, but this model's free limit is used up for now. Cygnus will use another."
    if re.search(r"\b404\b|not found|does not exist|no such model", low):
        return False, "The key works, but there is no model by that name. Check CYGNUS_MODEL."
    if re.search(r"connect|timed out|timeout|getaddrinfo|name resolution", low):
        return False, "Could not reach the provider. Check the internet connection."
    return False, "The call failed: " + error.splitlines()[0][:160]


def test(provider: str, key: str, model: str = "", base_url: str = "") -> dict:
    """One tool call with this key. Never returns or logs the key."""
    from openai import OpenAI

    from cygnus import config
    from cygnus.brain import router_health
    if provider not in config.PROVIDERS:
        return {"ok": False, "message": f"Unknown provider: {provider}."}
    if config.PROVIDERS[provider].get("key") and not key.strip():
        return {"ok": False, "message": "Paste the key first."}
    base, model = endpoint(provider, model, base_url)
    if not base or not model:
        return {"ok": False, "message": "This provider needs the endpoint address and the model name."}
    client = OpenAI(base_url=base, api_key=key.strip() or "none", timeout=40, max_retries=0)
    start = time.monotonic()
    try:
        reply = client.chat.completions.create(model=model, max_tokens=200, tools=router_health._PROBE_TOOL,
                                               tool_choice="auto", messages=router_health._PROBE_MESSAGES)
    except Exception as e:
        works, message = _plain(f"{type(e).__name__}: {e}")
        return {"ok": works, "model": model, "seconds": round(time.monotonic() - start, 1), "message": message}
    seconds = round(time.monotonic() - start, 1)
    if reply.choices and reply.choices[0].message.tool_calls:
        return {"ok": True, "model": model, "seconds": seconds, "message": f"It works: {model} answered in {seconds} s."}
    return {"ok": False, "model": model, "seconds": seconds,
            "message": f"{model} answered but did not use a tool. Cygnus needs a model that supports tools."}


def _env_file():
    from cygnus import paths
    env = paths.ENV_FILE
    if not env.exists():
        example = env.with_name(".env.example")
        if example.exists():
            shutil.copyfile(example, env)
        else:
            env.touch()
    return env


def save(provider: str, key: str = "", model: str = "", base_url: str = "") -> dict:
    """Keep the choice: the key in Credential Manager (or .env when that is
    not available), the provider and model in .env. Takes effect on restart."""
    from dotenv import set_key
    env = _env_file()
    where = ".env"
    if key.strip():
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, provider, key.strip())
            set_key(env, "CYGNUS_API_KEY", "", quote_mode="never")   # .env would win over the vault
            where = "Windows Credential Manager"
        except Exception:
            set_key(env, "CYGNUS_API_KEY", key.strip(), quote_mode="never")
    set_key(env, "CYGNUS_PROVIDER", provider, quote_mode="never")
    set_key(env, "CYGNUS_MODEL", model.strip(), quote_mode="never")
    if base_url.strip():
        set_key(env, "ROUTER_BASE_URL", base_url.strip(), quote_mode="never")
    return {"ok": True, "key_in": where if key.strip() else None, "restart": True,
            "message": "Saved. Restart Cygnus to use it."}
