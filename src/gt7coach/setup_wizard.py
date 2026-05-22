"""``gt7coach-setup`` — one-shot interactive wizard.

Replaces six README steps (clone, venv, edit .env, edit config.yaml,
test connectivity, run coach with flags) with a single linear flow:

    1. PS5 detection (broadcast discovery; fallback to manual IP).
    2. Pick LLM provider with a 4-line cheat sheet on trade-offs.
    3. Paste API key (masked); immediate 1-token smoke test against
       the provider so a bad key is caught here, not at race start.
    4. Pick voice engine; enumerate SAPI voices on Windows; speak a
       test phrase.
    5. Driver-style + car-class defaults.
    6. Write .env + config.yaml in the current directory, backing up
       any existing files with a timestamped suffix.

Pure stdlib + PyYAML + python-dotenv (both already in the project's
runtime deps). No new dependencies. Reuses (never reimplements):

* :func:`gt7coach.telemetry.receiver.discover_ps5`
* :func:`gt7coach.coach.providers.make_provider`
* :func:`gt7coach.voice.make_voice`
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gt7coach.setup")


# ---- Provider cheat sheet ---------------------------------------------------


@dataclass(frozen=True)
class _ProviderInfo:
    name: str
    env_var: str
    needs_key: bool
    one_liner: str
    extra_hint: str


_PROVIDERS: tuple[_ProviderInfo, ...] = (
    _ProviderInfo(
        name="gemini",
        env_var="GEMINI_API_KEY",
        needs_key=True,
        one_liner="Free tier (20 req/day, paid ~$0.10 per 1k corners). Fast.",
        extra_hint="Get a key at https://aistudio.google.com/app/apikey",
    ),
    _ProviderInfo(
        name="anthropic",
        env_var="ANTHROPIC_API_KEY",
        needs_key=True,
        one_liner="Best coaching quality. Paid (no free tier).",
        extra_hint="Get a key at https://console.anthropic.com/settings/keys",
    ),
    _ProviderInfo(
        name="openai",
        env_var="OPENAI_API_KEY",
        needs_key=True,
        one_liner="Similar quality to Anthropic. Paid.",
        extra_hint="Get a key at https://platform.openai.com/api-keys",
    ),
    _ProviderInfo(
        name="ollama",
        env_var="",
        needs_key=False,
        one_liner="Local, free, no quota. Slower (depends on your CPU/GPU).",
        extra_hint="Install Ollama from https://ollama.com then `ollama pull llama3.1:8b`",
    ),
    _ProviderInfo(
        name="mock",
        env_var="",
        needs_key=False,
        one_liner="No LLM call. Speaks canned phrases. Useful for testing the pipeline.",
        extra_hint="",
    ),
)


def _provider_by_name(name: str) -> _ProviderInfo | None:
    for p in _PROVIDERS:
        if p.name == name:
            return p
    return None


# ---- Wizard state ----------------------------------------------------------


@dataclass
class _WizardResult:
    ps5_ip: str  # "auto" or dotted-quad
    provider: str
    api_key: str | None  # None for ollama/mock
    voice_engine: str  # "pyttsx3" | "piper" | "null"
    voice_speed: int
    voice_name: str | None  # pyttsx3 SAPI voice id, or None to use default
    driver_style: str  # smooth | aggressive | learning
    car_class: str  # may be empty


# ---- UI helpers ------------------------------------------------------------


def _print_banner() -> None:
    print()
    print("=" * 64)
    print(" GT7 AI Coach — setup wizard")
    print("=" * 64)
    print()


def _section(title: str) -> None:
    print()
    print(f"-- {title} " + "-" * max(0, 60 - len(title)))


def _prompt(label: str, default: str | None = None) -> str:
    if default is not None:
        suffix = f" [{default}]: "
    else:
        suffix = ": "
    raw = input(label + suffix).strip()
    if not raw and default is not None:
        return default
    return raw


def _prompt_yn(label: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        raw = input(label + suffix).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer y or n.")


def _prompt_choice(label: str, choices: list[str], default: str | None = None) -> str:
    """Number-driven choice picker. Returns the chosen string."""
    print(f"{label}")
    for i, c in enumerate(choices, start=1):
        marker = " (default)" if c == default else ""
        print(f"  {i}. {c}{marker}")
    while True:
        raw = input("Choose [number or name]: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        if raw in choices:
            return raw
        print(f"Invalid choice. Pick 1-{len(choices)} or one of {choices}.")


# ---- Steps -----------------------------------------------------------------


def _step_ps5(skip_discovery: bool = False) -> str:
    _section("Step 1/6: PS5 detection")
    print("Make sure your PS5 is on and ideally in a race lobby")
    print("(Sport, Championship, time trial — telemetry must be flowing).")
    print()

    if skip_discovery or not _prompt_yn("Try to auto-discover the PS5 on this LAN?", default=True):
        ip = _prompt("PS5 IP address (or 'auto' to discover at runtime)", default="auto")
        return ip

    try:
        from gt7coach.telemetry.receiver import ReceiverConfig, discover_ps5

        cfg = ReceiverConfig(ps5_ip="auto", discovery_timeout=3.0)
        print("Scanning… (3 s broadcast + subnet fallback)")
        ip = discover_ps5(cfg)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("discovery raised: %s", exc)
        ip = None

    if ip:
        print(f"  Found PS5 at {ip}")
        if _prompt_yn("Use this IP?", default=True):
            return ip
    else:
        print("  No PS5 found (this is fine if GT7 isn't running yet).")

    return _prompt("PS5 IP address (or 'auto' to discover at runtime)", default="auto")


def _step_provider() -> tuple[str, str | None]:
    _section("Step 2/6: LLM provider")
    print("Picks which AI generates the coaching advice. Cheat sheet:")
    print()
    for p in _PROVIDERS:
        print(f"  {p.name:<10}  {p.one_liner}")
    print()
    provider = _prompt_choice(
        "Which provider?",
        [p.name for p in _PROVIDERS],
        default="gemini",
    )
    info = _provider_by_name(provider)
    assert info is not None
    if info.extra_hint:
        print(f"  Note: {info.extra_hint}")
    if not info.needs_key:
        return provider, None

    # Step 3 wrapped here so it loops on bad key without renumbering.
    return _step_api_key(provider)


def _step_api_key(provider: str) -> tuple[str, str]:
    info = _provider_by_name(provider)
    assert info is not None
    _section(f"Step 3/6: {provider.title()} API key")
    existing = os.environ.get(info.env_var)
    if existing:
        masked = existing[:6] + "…" + existing[-4:] if len(existing) > 12 else "(short key)"
        if _prompt_yn(f"Found {info.env_var}={masked} in env. Use it?", default=True):
            key = existing
            if _smoke_test(provider, key):
                return provider, key
            print("Smoke test failed with the env-var key. Paste a fresh one.")

    while True:
        key = getpass.getpass(f"Paste your {info.env_var} (input hidden): ").strip()
        if not key:
            print("  Empty key. Try again.")
            continue
        if _smoke_test(provider, key):
            return provider, key
        if not _prompt_yn("Try a different key?", default=True):
            print("  Continuing without a verified key. The coach will fall back")
            print("  to canned phrases until you fix it. You can edit .env later.")
            return provider, key


def _smoke_test(provider: str, key: str) -> bool:
    print(f"  Testing {provider} with a 1-token call…")
    try:
        from gt7coach.coach.providers import make_provider

        # set env so make_provider picks it up if it reads from env
        info = _provider_by_name(provider)
        if info is not None and info.env_var:
            os.environ[info.env_var] = key
        p = make_provider(provider, api_key=key)
        t0 = time.monotonic()
        resp = p.complete("You are a test.", "Say OK.", max_tokens=8)
        dt = (time.monotonic() - t0) * 1000
        if resp and len(resp.strip()) > 0:
            print(f"  OK ({dt:.0f} ms): {resp.strip()!r}")
            return True
        print(f"  Provider returned empty response ({dt:.0f} ms).")
        return False
    except Exception as exc:
        print(f"  Provider error: {exc}")
        return False


def _step_voice() -> tuple[str, int, str | None]:
    _section("Step 4/6: Voice")
    print("Which TTS engine should speak the coaching?")
    available = ["pyttsx3", "null"]
    try:
        import piper  # noqa: F401

        available.insert(1, "piper")
    except ImportError:
        pass
    engine = _prompt_choice("Engine", available, default="pyttsx3")

    if engine == "null":
        return engine, 200, None

    speed_raw = _prompt("Speech rate in words/minute (180-260)", default="200")
    try:
        speed = max(120, min(320, int(speed_raw)))
    except ValueError:
        speed = 200

    voice_name: str | None = None
    if engine == "pyttsx3":
        voice_name = _pick_pyttsx3_voice()

    if _prompt_yn("Speak a test phrase now?", default=True):
        _speak_test(engine, speed, voice_name)

    return engine, speed, voice_name


def _pick_pyttsx3_voice() -> str | None:
    try:
        import pyttsx3
    except ImportError:
        print("  pyttsx3 isn't installed. Install with: pip install 'gt7coach[voice]'")
        return None
    try:
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        engine.stop()
    except Exception as exc:
        print(f"  Couldn't enumerate SAPI voices ({exc}). Will use system default.")
        return None
    if not voices:
        return None
    print("  Available SAPI voices:")
    for i, v in enumerate(voices, start=1):
        name = getattr(v, "name", "(unnamed)")
        lang = ",".join(getattr(v, "languages", []) or []) or "-"
        print(f"    {i}. {name}  ({lang})")
    raw = _prompt("Pick a voice number (or blank for default)", default="")
    if raw and raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(voices):
            return voices[idx - 1].id
    return None


def _speak_test(engine: str, speed: int, voice_name: str | None) -> None:
    try:
        from gt7coach.voice import make_voice

        voice = make_voice(engine, rate=speed)
        # Apply selected pyttsx3 voice if provided — pyttsx3 engine doesn't
        # take voice_id in our factory, so set it post-construction.
        if engine == "pyttsx3" and voice_name:
            try:
                voice._voice_id = voice_name  # type: ignore[attr-defined]
            except Exception:
                pass
        print("  Speaking…")
        voice.speak("Coach ready. Testing voice.")
        # Wait for the queue to drain (bounded).
        deadline = time.monotonic() + 8.0
        while not voice.is_idle() and time.monotonic() < deadline:
            time.sleep(0.05)
        voice.stop()
    except Exception as exc:
        print(f"  Voice failed: {exc}")


def _step_style_and_car() -> tuple[str, str]:
    _section("Step 5/6: Driver style + car class")
    style = _prompt_choice(
        "Driver style",
        ["smooth", "aggressive", "learning"],
        default="smooth",
    )
    print("car_class is a free-form string spliced into every coaching prompt")
    print("so the AI can tailor advice to your drivetrain / power tier.")
    print("Examples: 'Gr.3 RWD', 'Gr.4 AWD', 'N300 FWD', 'Vintage GT3'.")
    car = _prompt("Default car class (blank to skip)", default="")
    return style, car


def _step_write_files(result: _WizardResult, target_dir: Path) -> tuple[Path, Path]:
    _section("Step 6/6: Writing .env and config.yaml")
    target_dir.mkdir(parents=True, exist_ok=True)
    env_path = target_dir / ".env"
    cfg_path = target_dir / "config.yaml"

    _backup_if_exists(env_path)
    _backup_if_exists(cfg_path)

    _write_env_file(env_path, result)
    _write_config_yaml(cfg_path, result)

    print(f"  Wrote {env_path}")
    print(f"  Wrote {cfg_path}")
    return env_path, cfg_path


def _backup_if_exists(path: Path) -> None:
    if not path.exists():
        return
    suffix = time.strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{suffix}")
    path.rename(backup)
    print(f"  Backed up existing {path.name} to {backup.name}")


def _write_env_file(path: Path, r: _WizardResult) -> None:
    lines = [
        "# Written by gt7coach-setup.",
        "# Add additional API keys here if you want to switch providers later.",
        "",
    ]
    info = _provider_by_name(r.provider)
    if info is not None and info.needs_key and r.api_key:
        lines.append(f"{info.env_var}={r.api_key}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_config_yaml(path: Path, r: _WizardResult) -> None:
    car_class_line = f'  car_class: "{r.car_class}"' if r.car_class else '  # car_class: "Gr.3 RWD"'
    yaml_text = f"""# Written by gt7coach-setup. Edit anytime; CLI flags still override.

network:
  ps5_ip: {r.ps5_ip}
  port_rx: 33740
  port_tx: 33739
  heartbeat_seconds: 1.7
  packet_format: B

coach:
  provider: {r.provider}
  driver_style: {r.driver_style}
  global_rate_limit_seconds: 4
{car_class_line}

voice:
  engine: {r.voice_engine}
  speed: {r.voice_speed}

session:
  log_dir: ./sessions
  generate_summary: true
"""
    path.write_text(yaml_text, encoding="utf-8")


# ---- Entry point -----------------------------------------------------------


def run(target_dir: Path | None = None, *, skip_discovery: bool = False) -> _WizardResult:
    """Run the wizard interactively. Returns the chosen settings."""
    _print_banner()
    target_dir = target_dir or Path.cwd()
    print(f"This will write .env and config.yaml into:  {target_dir}")
    print()

    ps5_ip = _step_ps5(skip_discovery=skip_discovery)
    provider, api_key = _step_provider()
    voice_engine, voice_speed, voice_name = _step_voice()
    driver_style, car_class = _step_style_and_car()

    result = _WizardResult(
        ps5_ip=ps5_ip,
        provider=provider,
        api_key=api_key,
        voice_engine=voice_engine,
        voice_speed=voice_speed,
        voice_name=voice_name,
        driver_style=driver_style,
        car_class=car_class,
    )
    _step_write_files(result, target_dir)

    _section("Done")
    print("Setup complete. Try:  gt7coach-coach")
    print("(Or  gt7coach-gui  if you installed the GUI extra.)")
    print()
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gt7coach-setup",
        description="Interactive one-shot setup for GT7 AI Coach.",
    )
    p.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="Directory to write .env + config.yaml into (default: cwd).",
    )
    p.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip the PS5 broadcast scan (use if you already know the IP).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        run(target_dir=args.target_dir, skip_discovery=args.skip_discovery)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
