"""
ITU ACM Instants — web server
Upload images → get ready-to-post 1080×1350 Instagram frames back.

Single image  → returns a PNG directly.
Multiple      → returns a ZIP (same stacking logic as the CLI).

  uvicorn server:app --reload        # dev
  uvicorn server:app --host 0.0.0.0  # prod / Docker
"""

import base64
import io
import json
import random
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from playwright.async_api import async_playwright
from starlette.background import BackgroundTask

from main import CONFIG, EMOJI_SETS, to_data_uri

BASE = Path(__file__).parent
TMP  = BASE / "tmp"
TMP.mkdir(exist_ok=True)

app = FastAPI(title="ITU ACM Instants")


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE / "static" / "index.html").read_text()


# ── Rendering ─────────────────────────────────────────────────────────────────

async def _render_batch(img_paths: list[Path], out_dir: Path, custom: dict) -> list[Path]:
    """
    Render each image with the next 2 peeking behind it — identical to the CLI.
    Transforms are generated fresh per session (not persisted to events.json).
    custom = { handle, time, accent, icon_uri }
    """
    # Stable transforms for this batch session (not persisted — ephemeral uploads)
    transforms = {
        f.stem: {
            "rotation": round(random.uniform(-12, 12), 1),
            "tx":       round(random.uniform(-15, 65), 1),
            "ty":       round(random.uniform(-60, 0),  1),
        }
        for f in img_paths
    }

    icon_uri     = custom["icon_uri"]
    template_url = "file://" + str(CONFIG["template"])
    outputs: list[Path] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        page = await browser.new_page(
            viewport={"width": 1080, "height": 1350},
            device_scale_factor=2,
        )

        for idx, file in enumerate(img_paths):
            # Current = front; next 2 peek behind. No wrap — last image is alone.
            stack = img_paths[idx : idx + 3]
            n     = len(stack)

            epp = custom["emojis_per_photo"]
            settings = {
                "handle": custom["handle"],
                "time":   custom["time"],
                "accent": custom["accent"],
                "emojis": (epp[idx] if epp and idx < len(epp) else None)
                          or EMOJI_SETS[idx % len(EMOJI_SETS)],
            }

            # DOM order: deepest back first → front last.
            # Front card always sits centered; back cards use their stable offsets.
            dom_transforms = [
                {
                    "r":  transforms[sf.stem]["rotation"],
                    "tx": transforms[sf.stem]["tx"],
                    "ty": transforms[sf.stem]["ty"],
                }
                for sf in reversed(stack)
            ]

            await page.goto(template_url, wait_until="networkidle")
            await page.evaluate("() => document.fonts.ready")

            # Pass 1 — build empty card structure
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
                    } else {
                        document.getElementById('aIcon').innerHTML =
                            '<span id="aIconText">ACM</span>';
                    }
                    const el = document.getElementById('cardStack');
                    el.innerHTML = '';
                    for (let i = 0; i < n; i++) {
                        const isFront = i === n - 1;
                        const t  = transforms[i];
                        const tx = isFront ? 0 : t.tx;
                        const ty = isFront ? 0 : t.ty;
                        const card = document.createElement('div');
                        card.className = 'card';
                        card.style.transform =
                            `rotate(${t.r}deg) translate(${tx.toFixed(1)}px, ${ty.toFixed(1)}px)`;
                        card.appendChild(document.createElement('img'));
                        el.appendChild(card);
                    }
                }""",
                [settings, n, dom_transforms, icon_uri],
            )

            # Pass 2 — inject images one at a time (avoids large IPC payloads)
            for i, sf in enumerate(stack):
                dom_idx = n - 1 - i   # stack[0] = front → DOM[n-1]
                await page.locator(
                    f"#cardStack .card:nth-child({dom_idx + 1}) img"
                ).evaluate("(img, src) => img.src = src", to_data_uri(sf))

            await page.wait_for_timeout(250)
            out_path = out_dir / f"{file.stem}-instant.png"
            await page.locator("#stage").screenshot(path=str(out_path))
            outputs.append(out_path)

        await browser.close()

    return outputs


def _zip_files(paths: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, p.name)
    return buf.getvalue()


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/generate")
async def generate(
    files:  List[UploadFile]        = File(...),
    handle: str                     = Form(default=""),
    time:   str                     = Form(default=""),
    accent: str                     = Form(default=""),
    emojis_json: str                = Form(default=""),
    icon:   Optional[UploadFile]    = File(default=None),
):
    if not files:
        raise HTTPException(400, "No files uploaded.")
    for f in files:
        if not (f.content_type or "").startswith("image/"):
            raise HTTPException(400, f"{f.filename} is not an image.")

    # Resolve custom settings — fall back to server defaults when empty
    cfg_handle = handle.strip() or CONFIG["handle"]
    cfg_time   = time.strip()   or CONFIG["time"]
    cfg_accent = accent.strip() or CONFIG["accent"]

    # Per-photo emoji sets — list of [e0,e1,e2,e3] per uploaded image
    cfg_emojis_per_photo: list | None = None
    if emojis_json.strip():
        try:
            parsed = json.loads(emojis_json)
            if isinstance(parsed, list):
                cfg_emojis_per_photo = [
                    [(e or "⚡") for e in (row[:4] + ["⚡"] * 4)[:4]]
                    for row in parsed
                ]
        except Exception:
            pass  # fall back to EMOJI_SETS cycling

    # Icon: uploaded image → data URI; else fall back to server asset
    cfg_icon_uri: Optional[str] = None
    if icon and icon.filename:
        icon_bytes = await icon.read()
        ext  = Path(icon.filename).suffix.lstrip(".").lower()
        mime = "jpeg" if ext == "jpg" else ext
        b64  = base64.b64encode(icon_bytes).decode()
        cfg_icon_uri = f"data:image/{mime};base64,{b64}"
    elif CONFIG["icon_path"].exists():
        cfg_icon_uri = to_data_uri(CONFIG["icon_path"])

    custom = {
        "handle":   cfg_handle,
        "time":     cfg_time,
        "accent":   cfg_accent,
        "icon_uri": cfg_icon_uri,
        "emojis_per_photo": cfg_emojis_per_photo,  # None → cycle EMOJI_SETS
    }

    job     = uuid.uuid4().hex[:8]
    job_dir = TMP / job
    in_dir  = job_dir / "in"
    out_dir = job_dir / "out"
    in_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    # Save uploads in the order they were sent
    saved: list[Path] = []
    for upload in files:
        dest = in_dir / f"{len(saved):04d}_{upload.filename}"
        with open(dest, "wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved.append(dest)

    def cleanup():
        shutil.rmtree(job_dir, ignore_errors=True)

    try:
        outputs = await _render_batch(saved, out_dir, custom)
    except Exception as exc:
        cleanup()
        raise HTTPException(500, f"Render failed: {exc}") from exc

    if not outputs:
        cleanup()
        raise HTTPException(500, "No outputs were produced.")

    if len(outputs) == 1:
        original_stem = Path(files[0].filename or "instant").stem
        return FileResponse(
            outputs[0],
            media_type="image/png",
            filename=f"{original_stem}-instant.png",
            background=BackgroundTask(cleanup),
        )
    else:
        zip_bytes = _zip_files(outputs)
        cleanup()
        return Response(
            zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="instants.zip"'},
        )
