"""Store (MSIX) apps and UWP windows: the registry scan learns Telegram,
Calculator and Windows Terminal from the Start menu; the Running panel gives
a Store app's frame window to the app itself; "close it" finds an app the
registry does not know through the Running panel (owner, 2026-09-15)."""
import machine_capabilities as mc
import running_apps as ra
import tools
import app_commands


# ------------------------------------------------------------ running --

def test_a_store_apps_frame_window_belongs_to_the_app():
    found = {13572: [(100, "Settings"), (101, "Realtek Audio Console")], 4444: [(7, "New Tab - Chrome")]}
    frames = {100: 13572, 101: 13572}
    cores = {"Settings": 11068, "Realtek Audio Console": 7636, "Windows Input Experience": 29328}
    out = ra._adopt_frames(found, frames, cores)
    assert out == {11068: [(100, "Settings")], 7636: [(101, "Realtek Audio Console")],
                   4444: [(7, "New Tab - Chrome")]}


def test_a_frame_with_no_core_window_stays_with_its_host():
    found = {13572: [(100, "Settings")]}
    assert ra._adopt_frames(found, {100: 13572}, {}) == {13572: [(100, "Settings")]}


def test_settings_is_a_row_now():
    assert "systemsettings.exe" not in ra._SKIP


# ------------------------------------------------------------ registry --

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">
  <Applications>
    <Application Id="App" Executable="CalculatorApp.exe" EntryPoint="x"/>
    <Application Id="Other" Executable="bin\\Helper.exe"/>
  </Applications>
</Package>"""


def test_manifest_exe_by_application_id():
    assert mc._manifest_exe(MANIFEST, "App") == "CalculatorApp.exe"
    assert mc._manifest_exe(MANIFEST, "Other") == "Helper.exe"
    assert mc._manifest_exe(MANIFEST, "missing") == "CalculatorApp.exe"
    assert mc._manifest_exe("", "App") is None
    assert mc._manifest_exe("<not xml", "App") is None


def test_store_entries_from_start_menu_and_packages():
    start = [{"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
             {"Name": "Telegram", "AppID": "TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm!Telegram.TelegramDesktop.Store"},
             {"Name": "Feedback Hub", "AppID": "Microsoft.WindowsFeedbackHub_8wekyb3d8bbwe!App"},
             {"Name": "Obsidian", "AppID": "C:\\Users\\x\\Obsidian.exe"}]
    packages = [{"Name": "Microsoft.WindowsCalculator", "PackageFamilyName": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
                 "InstallLocation": "C:\\WA\\calc"},
                {"Name": "TelegramMessengerLLP.TelegramDesktop", "PackageFamilyName": "TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm",
                 "InstallLocation": "C:\\WA\\tg"}]
    manifests = {"C:\\WA\\calc\\AppxManifest.xml": MANIFEST,
                 "C:\\WA\\tg\\AppxManifest.xml": MANIFEST.replace("CalculatorApp.exe", "Telegram.exe")
                 .replace('Id="App"', 'Id="Telegram.TelegramDesktop.Store"')}
    out = mc._store_entries(start, packages, manifests.get)
    assert set(out) == {"calculator", "telegram", "feedback hub"}      # the exe path is the .lnk scan's
    calc = out["calculator"]
    assert calc["bin"] == "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert calc["exe"] == "CalculatorApp.exe" and calc["kind"] == "store"
    assert calc["label"] == "Calculator" and "hidden" not in calc
    assert out["telegram"]["exe"] == "Telegram.exe"
    assert out["feedback hub"]["hidden"] is True and out["feedback hub"]["hidden_by"] == "scan"


def test_store_bin_is_launchable_and_survives_the_write_filter():
    assert mc._is_launchable("shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    assert not mc._is_launchable("shell:AppsFolder\\")
    assert not mc._is_launchable("C:\\nowhere\\missing.exe")


# ----------------------------------------------------------- close --

def test_close_finds_an_unregistered_app_through_the_running_panel(monkeypatch):
    monkeypatch.setattr(tools, "_running_row_for", lambda said: {
        "name": "Windows Terminal", "exes": ["openconsole.exe", "windowsterminal.exe"], "pids": [1]})
    seen = []

    def fake_run(cmd, *a, **k):
        seen.append(cmd)

        class R:
            stdout = "WindowsTerminal.exe  29048 Console" if cmd[0] == "tasklist" else ""
            returncode = 0
        return R()
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    import machine_capabilities as _mc
    monkeypatch.setattr(_mc, "load_registry", lambda: {"apps": {}})
    monkeypatch.setattr(_mc, "resolve", lambda n: None)
    out = tools.close_application("close windows terminal")
    assert "Windows Terminal" in out
    assert any(c[0] == "tasklist" and "windowsterminal.exe" in c[-1].lower() for c in seen)


def test_row_exe_prefers_the_program_named_like_the_row():
    assert tools._row_exe({"name": "Google Chrome", "exes": ["chrome-native-host.exe", "chrome.exe", "cmd.exe"]}) == "chrome"
    assert tools._row_exe({"name": "Telegram", "exes": ["telegram.exe"]}) == "telegram"
    assert tools._row_exe({"name": "Windows Terminal", "exes": ["openconsole.exe", "windowsterminal.exe"]}) == "windowsterminal"
    assert tools._row_exe({"name": "X", "exes": []}) == ""


def test_registry_keys_match_a_store_apps_exe():
    apps = {"telegram": {"bin": "shell:AppsFolder\\T!App", "exe": "Telegram.exe"},
            "chrome": {"bin": "C:\\x\\chrome.exe"}}
    row = {"exes": ["telegram.exe"]}
    assert app_commands._registry_keys(row, apps) == ["telegram"]


# ------------------------------------------------------------ close it --

def test_close_it_means_the_app_just_opened(monkeypatch):
    import dialogue_state
    dialogue_state.reset()
    tools._note_opened("Calculator", {"label": "Calculator"})
    seen = []
    monkeypatch.setattr(tools, "_running_row_for", lambda said: None)

    def fake_run(cmd, *a, **k):
        seen.append(cmd)

        class R:
            stdout = "" if cmd[0] == "tasklist" else ""
            returncode = 0
        return R()
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    import machine_capabilities as _mc
    monkeypatch.setattr(_mc, "load_registry", lambda: {"apps": {"calculator": {
        "bin": "shell:AppsFolder\X!App", "exe": "CalculatorApp.exe", "label": "Calculator"}}})
    out = tools.close_application("close it")
    assert out == "Calculator is not running."
    assert any(c[0] == "tasklist" and "calculatorapp.exe" in c[-1].lower() for c in seen)


def test_close_it_with_nothing_opened_asks(monkeypatch):
    import dialogue_state
    dialogue_state.reset()
    assert tools.close_application("close it").startswith("[Error] Which app")
