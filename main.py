#!/usr/bin/env python3
"""ITU ACM "Instants" post generator — BATCH

Drop several event images into ./events, run once,
get all of them as ready-to-post 1080x1350 PNGs in ./outputs.
Each image gets its own emoji set, cycling through EMOJI_SETS below.

  python main.py                  # process EVERY image in ./events
  python main.py poster.png       # process a single file
  python main.py --watch          # re-run automatically when events/ changes

GLOBAL defaults live in CONFIG below (edit once).
EMOJI_SETS cycles automatically — edit or extend to taste.

PER-EVENT overrides (optional): create ./events.json like:
  {
    "amazon-visit":   { "emojis": ["📦","🚀","💼","🔥"] },
    "data-workshop":  { "emojis": ["📊","🐍","🤖","⚡"], "time": "tomorrow" },
    "compiler-talk":  { "emojis": ["🛠️","💻","🧠","🔥"] }
  }
Keys = the image filename WITHOUT extension. Anything omitted falls back to CONFIG / EMOJI_SETS.
"""

import asyncio
import base64
import json
import random
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = Path(__file__).parent

CONFIG = {
    "handle": "ituacmsc",
    "time": "now",
    "accent": "#f5b301",
    "icon_path": BASE / "assets" / "icon.jpg",
    "events_dir": BASE / "events",
    "outputs_dir": BASE / "outputs",
    "overrides_file": BASE / "events.json",
    "template": BASE / "template.html",
}

# One set per image, cycling when there are more images than sets.
# [left, top, bottom-left, bottom-right] — right bubble is always "+".
# events.json per-event "emojis" key always takes priority.
EMOJI_SETS = [
    ["💻", "⚡", "🤖", "🔥"],
    ["📦", "🚀", "💼", "🌟"],
    ["📊", "🐍", "🧠", "⚡"],
    ["🛠️", "🔧", "💡", "🎯"],
    ["🎮", "🕹️", "👾", "🚀"],
    ["🔐", "🛡️", "🔍", "💡"],
    ["🌐", "☁️", "📡", "🔗"],
    ["📱", "💬", "🎨", "✨"],
]

VALID_EXT = {".png", ".jpg", ".jpeg", ".webp"}
WATCH_INTERVAL = 2  # seconds between polls


def to_data_uri(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    mime = "jpeg" if ext == "jpg" else ext
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/{mime};base64,{b64}"


def save_overrides(data: dict) -> None:
    CONFIG["overrides_file"].write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_overrides() -> dict:
    f = CONFIG["overrides_file"]
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError as e:
        print(f"⚠ events.json is not valid JSON — ignoring it. ({e})")
        return {}


def dir_state(events_dir: Path) -> str:
    """Fingerprint of the events folder — changes when files are added/removed/modified."""
    files = sorted(
        f for f in events_dir.iterdir() if f.suffix.lower() in VALID_EXT
    )
    return str([(f.name, f.stat().st_mtime) for f in files])


def collect_files(events_dir: Path, arg: str | None) -> list[Path]:
    if arg and arg != "--watch":
        p = Path(arg)
        return [p if p.is_absolute() else events_dir / arg]
    events_dir.mkdir(parents=True, exist_ok=True)
    ov = load_overrides()
    all_files = [f for f in events_dir.iterdir() if f.suffix.lower() in VALID_EXT]
    return sorted(
        all_files,
        key=lambda f: (ov.get(f.stem, {}).get("order", float("inf")), f.name),
    )


def ensure_transforms(files: list[Path], overrides: dict) -> dict:
    """Assign stable rotation + back-card offset to every image that lacks them, then persist."""
    changed = False
    for file in files:
        entry = overrides.setdefault(file.stem, {})
        if "rotation" not in entry:
            entry["rotation"] = round(random.uniform(-12, 12), 1)
            changed = True
        if "tx" not in entry:
            # Horizontal peek offset when behind (slightly right of center)
            entry["tx"] = round(random.uniform(-15, 65), 1)
            changed = True
        if "ty" not in entry:
            # Vertical peek offset when behind (slightly up)
            entry["ty"] = round(random.uniform(-60, 0), 1)
            changed = True
    if changed:
        save_overrides(overrides)
    return overrides


async def render_once(page, files: list[Path], template_url: str, outputs_dir: Path) -> None:
    """Render each file individually — one output PNG per image."""
    overrides = load_overrides()
    overrides = ensure_transforms(files, overrides)
    icon_path: Path = CONFIG["icon_path"]
    icon_uri = to_data_uri(icon_path) if icon_path.exists() else None

    print(f"Generating {len(files)} post(s)...\n")
    ok = failed = 0

    for idx, file in enumerate(files):
        name = file.stem
        try:
            ov = overrides.get(name, {})
            settings = {
                "handle": ov.get("handle", CONFIG["handle"]),
                "time":   ov.get("time",   CONFIG["time"]),
                "accent": ov.get("accent", CONFIG["accent"]),
                "emojis": ov.get("emojis", EMOJI_SETS[idx % len(EMOJI_SETS)]),
            }

            # Current image is front; next 2 peek behind it.
            # No wrapping — last image shows alone, second-to-last shows 2 cards.
            stack_files = files[idx:idx + 3]
            n = len(stack_files)

            await page.goto(template_url, wait_until="networkidle")
            await page.evaluate("() => document.fonts.ready")

            # Transforms in DOM order: stack_files[-1] (deepest back) → DOM[0],
            # stack_files[0] (front) → DOM[n-1].
            # Front card always renders at center (tx=0, ty=0); back cards use
            # their stored offsets so they appear at the same position every render.
            dom_transforms = []
            for sf in reversed(stack_files):
                ov_sf = overrides[sf.stem]
                dom_transforms.append({
                    "r":  ov_sf["rotation"],
                    "tx": ov_sf["tx"],
                    "ty": ov_sf["ty"],
                })

            # Pass 1 — build empty card DOM structure using stored transforms.
            await page.evaluate(
                """([s, n, transforms, iconUri]) => {
                    document.documentElement.style.setProperty('--accent', s.accent);
                    document.getElementById('aHandle').textContent = s.handle;
                    document.getElementById('aTime').textContent   = s.time;
                    document.getElementById('b0').textContent = s.emojis[0];
                    document.getElementById('b1').textContent = s.emojis[1];
                    document.getElementById('b2').textContent = s.emojis[2];
                    document.getElementById('b3').textContent = s.emojis[3];
                    if (iconUri) {
                        document.getElementById('aIcon').innerHTML =
                            '<img src="' + iconUri + '" alt="ACM">';
                    }
                    const stack = document.getElementById('cardStack');
                    stack.innerHTML = '';
                    for (let i = 0; i < n; i++) {
                        const isFront = i === n - 1;
                        const t  = transforms[i];
                        // Front card always sits centered; back cards use their
                        // stable stored offsets so position never changes across renders.
                        const tx = isFront ? 0 : t.tx;
                        const ty = isFront ? 0 : t.ty;
                        const card = document.createElement('div');
                        card.className = 'card';
                        card.style.transform =
                            `rotate(${t.r}deg) translate(${tx.toFixed(1)}px, ${ty.toFixed(1)}px)`;
                        card.appendChild(document.createElement('img'));
                        stack.appendChild(card);
                    }
                    // DOM[0] = deepest back, DOM[n-1] = front
                }""",
                [settings, n, dom_transforms, icon_uri],
            )

            # Pass 2 — inject images one at a time (front = stack_files[0]).
            for i, sf in enumerate(stack_files):
                dom_idx = n - 1 - i  # stack_files[0] (front) → DOM[n-1]
                await page.locator(
                    f"#cardStack .card:nth-child({dom_idx + 1}) img"
                ).evaluate("(img, src) => img.src = src", to_data_uri(sf))

            await page.wait_for_timeout(250)
            out_path = outputs_dir / f"{name}-instant.png"
            await page.locator("#stage").screenshot(path=str(out_path))

            tag = "  (custom emojis)" if (name in overrides and "emojis" in overrides[name]) \
                  else f"  [{' '.join(settings['emojis'])}]"
            print(f"  ✓ {file.name}  →  outputs/{name}-instant.png{tag}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {file.name} failed: {e}")
            failed += 1

    suffix = f", {failed} failed" if failed else ""
    print(f"\nDone. {ok} generated{suffix} → ./outputs")


async def run_render(files: list[Path], template_url: str, outputs_dir: Path) -> None:
    """Spin up a fresh browser, render, then close. Used by watch mode so
    no browser state leaks between renders."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
        page = await browser.new_page(
            viewport={"width": 1080, "height": 1350},
            device_scale_factor=2,
        )
        await render_once(page, files, template_url, outputs_dir)
        await browser.close()


async def main():
    args = sys.argv[1:]
    watch_mode = "--watch" in args
    file_arg   = next((a for a in args if a != "--watch"), None)

    events_dir:  Path = CONFIG["events_dir"]
    outputs_dir: Path = CONFIG["outputs_dir"]
    outputs_dir.mkdir(parents=True, exist_ok=True)
    template_url = "file://" + str(CONFIG["template"])

    if watch_mode:
        print("👀  Watching events/ for changes. Ctrl+C to stop.\n")
        last_state = None
        try:
            while True:
                current_state = dir_state(events_dir) if events_dir.exists() else ""
                if current_state != last_state:
                    last_state = current_state
                    files = collect_files(events_dir, file_arg)
                    if files:
                        try:
                            await run_render(files, template_url, outputs_dir)
                        except Exception as e:
                            print(f"⚠  Render failed: {e}")
                        print("\n👀  Watching for changes...\n")
                    else:
                        print("No images in events/ yet — add some to generate.\n")
                await asyncio.sleep(WATCH_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        files = collect_files(events_dir, file_arg)
        if not files:
            print("No images found in ./events. Add some event posters and rerun.")
        else:
            await run_render(files, template_url, outputs_dir)


if __name__ == "__main__":
    asyncio.run(main())
