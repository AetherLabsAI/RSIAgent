"""Look-attachment preparation (v13) — fix the EYES mechanically, not the prompt.

A full-page technical image (A4 drawing at 300 dpi ≈ 2480x3508) gets downsampled by
VLM preprocessing to a working resolution where small callout text and thin line-work
become physically illegible — the model is not careless, it literally cannot see.
The harness therefore attaches, alongside a bounded overview, NATIVE-RESOLUTION
overlapping tiles of any large image (and performs requested region crops
server-side), so a look sees what a human sees when they zoom. No behavioral demand
on the model; degrades gracefully to pass-through when PIL is unavailable.
"""
import io
import logging
import re

log = logging.getLogger("forge.imagery")

MAX_SIDE = 1400          # overview / tile output bound (px, long side)
TILE_THRESHOLD = 1800    # images larger than this (long side) also get tiles
OVERLAP = 0.10           # tile overlap fraction (features on seams stay whole)
MAX_GRID_ATTACH = 6      # one vision-agent grid operation is deliberately <= 6 cells


# v32.5: when set (loop wires it from cfg.archive_eyes + the run's sink root), every
# encoded image handed to the eyes is also written here — results/<task>/<seed>/eyes/
# NNN_<label>.png|jpg — so a human can inspect EXACTLY what the model saw, per look.
# Motivated by the alpha-flatten bug hiding for 3 runs: text about images is not
# evidence about images; now the pixels themselves are part of the run artifacts.
ARCHIVE_DIR = None
_ARCHIVE_SEQ = [0]


def _archive(data: bytes, label: str) -> None:
    if not ARCHIVE_DIR:
        return
    try:
        import os
        import re as _re
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        _ARCHIVE_SEQ[0] += 1
        ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        slug = _re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60] or "img"
        with open(os.path.join(ARCHIVE_DIR,
                               f"{_ARCHIVE_SEQ[0]:03d}_{slug}.{ext}"), "wb") as f:
            f.write(data)
    except Exception:                                      # never break a look
        pass


def _flatten_alpha(img):
    """v32.4: flatten transparency onto a CONTRAST-AWARE background before the VLM
    sees it. Sending raw RGBA lets the provider flatten onto black, which DESTROYS
    dark-on-transparent content: t003's rain overlay (dark blue streaks on alpha)
    became near-invisible specks the eyes read as 'sparkle motifs on black' — a
    pipeline artifact we mistook for a semantic ceiling until a human referee viewed
    the image (2026-07-15). Background is chosen against the content's own
    luminance: dark content -> white, light content -> black. Returns
    (rgb_image, note); note is '' for opaque images."""
    from PIL import Image
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode not in ("RGBA", "LA"):
        return img, ""
    rgba = img.convert("RGBA")
    alpha = rgba.split()[-1]
    lum = rgba.convert("L")
    mask = alpha.point(lambda a: 255 if a > 32 else 0)
    stat = lum.histogram(mask)
    total = sum(stat) or 1
    mean_lum = sum(i * c for i, c in enumerate(stat)) / total
    bg_val = 255 if mean_lum < 128 else 0
    bg = Image.new("RGB", rgba.size, (bg_val,) * 3)
    bg.paste(rgba, mask=alpha)
    return bg, ("(transparent image flattened onto "
                + ("white" if bg_val else "black") + " for viewing)")


def _encode(img) -> bytes:
    img, _ = _flatten_alpha(img)
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.save(buf, format="PNG", optimize=True)
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _bounded(img, max_side: int = MAX_SIDE):
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    s = max_side / max(w, h)
    return img.resize((max(1, int(w * s)), max(1, int(h * s))))


def grid_cells(datas: list, rows: int, cols: int, cell=None):
    """v22.1 (vision-agent): split the FIRST image into a rows x cols grid and return
    native-resolution cell(s), bounded. ``cell=[r,c]`` -> that one cell (deep zoom for
    counting a section); ``cell=None`` -> all cells row-major (up to MAX_GRID_ATTACH), each
    labelled, so a VLM can count per-cell then sum. Returns (attachments, note)."""
    try:
        from PIL import Image
    except ImportError:
        return datas[:1], ""
    if not datas:
        return [], ""
    try:
        img = Image.open(io.BytesIO(datas[0])); img.load()
    except Exception:                                      # noqa: BLE001
        return datas[:1], ""
    rows = max(1, min(int(rows), 6)); cols = max(1, min(int(cols), 6))
    w, h = img.size
    cw, ch = w / cols, h / rows
    want = ([tuple(cell)] if (cell and len(cell) == 2)
            else [(r, c) for r in range(rows) for c in range(cols)])
    capped = cell is None and len(want) > MAX_GRID_ATTACH
    out, parts = [], []
    for (r, c) in want:
        if len(out) >= MAX_GRID_ATTACH:
            break
        r = max(0, min(int(r), rows - 1)); c = max(0, min(int(c), cols - 1))
        box = (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))
        out.append(_encode(_bounded(img.crop(box))))
        parts.append(f"row {r},col {c}")
    note = f"{rows}x{cols} grid, native-res cell(s): " + "; ".join(parts) + "."
    if capped:                                        # the agent prompt requires <= 6 grid cells
        note += (f" (ONLY {MAX_GRID_ATTACH} of {rows * cols} cells shown — use a "
                 f"COARSER grid of <= {MAX_GRID_ATTACH} cells to see them all at "
                 "once and sum reliably.)")
    for _d, _l in zip(out, parts):                         # v32.5: eyes archive
        _archive(_d, "grid_" + _l)
    return out, note


_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_RENDER_DIR = "/tmp/forge_render"
_RENDER_TIMEOUT = 120          # soffice cold-start can be 20-40s; give headroom


def fetch_look_image(vm, path: str):
    """v26: fetch ``path`` from the VM as IMAGE bytes, rendering documents automatically.

    A `look` needs an image, but a deliverable is often a document (a .pptx is a zip of
    XML, not a picture). Before v26 the CALLER had to render it — the inspector authored
    a render pipeline across STATELESS probes (each run_command is a fresh shell, so cwd
    never persisted) and either fumbled it or spent its whole probe budget on it (t063
    inspections @80/@82). Here the harness does it: an image path is returned as-is; a
    document is rendered to a PNG in-VM with ONE known-good, absolute-path command and
    that PNG is returned. So a caller can `look` straight at a deliverable file. Returns
    (bytes, err) exactly like ``vm.fetch_file`` (err '' on success). General machinery —
    the only knowledge is 'documents render to images', no task/format specifics.

    v29: a target of ``screen:`` (optionally ``screen:<display>``, default :0) captures
    the LIVE screen — the running browser/app window — to a PNG and returns that. Stateful
    web/app tasks keep their deliverable in session memory, not a file; without this the
    inspector can only probe the filesystem and mis-judges genuinely-completed work as
    absent (t021 drove both web apps + read the card, scored 0 because the verifier could
    not SEE the session). The harness runs ONE known-good capture; the caller just
    ``look``s at ``screen:``. Same general-machinery contract; no task knowledge."""
    low = path.lower()
    if low.startswith("screen:"):                          # v29: capture the LIVE screen
        disp = path.split(":", 1)[1].strip() or "0"
        if not re.fullmatch(r":?\d+(?:\.\d+)?", disp):
            return None, "invalid screen display; expected a numeric X display"
        disp = disp if disp.startswith(":") else f":{disp}"
        cap = (f"rm -rf {_RENDER_DIR} && mkdir -p {_RENDER_DIR} && "
               # try import(ImageMagick) then scrot then gnome-screenshot — first that exists
               f"(DISPLAY={disp} import -window root {_RENDER_DIR}/screen.png 2>/dev/null "
               f"|| DISPLAY={disp} scrot {_RENDER_DIR}/screen.png 2>/dev/null "
               f"|| DISPLAY={disp} gnome-screenshot -f {_RENDER_DIR}/screen.png 2>/dev/null); "
               f"ls {_RENDER_DIR}/screen.png 2>/dev/null")
        try:
            out = vm.run_command(cap, timeout=_RENDER_TIMEOUT)
        except Exception as e:                             # noqa: BLE001
            return None, f"screen capture failed ({type(e).__name__})"
        shot = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if not shot.endswith("screen.png"):
            return None, "could not capture the live screen (no display/tool)"
        return vm.fetch_file(shot)
    if low.endswith(_IMG_EXT):                             # already a picture
        return vm.fetch_file(path)
    q = "'" + path.replace("'", "'\\''") + "'"            # single-quote for the shell
    if low.endswith(".pdf"):
        conv = f"pdftoppm -png -f 1 -l 1 -r 120 {q} {_RENDER_DIR}/page"
    else:                                                 # office/vector formats -> soffice
        conv = ("soffice --headless "
                f"-env:UserInstallation=file://{_RENDER_DIR}/profile "   # own profile: no
                f"--convert-to png --outdir {_RENDER_DIR} {q}")          # lock vs a running
    cmd = (f"rm -rf {_RENDER_DIR} && mkdir -p {_RENDER_DIR} && {conv} >/dev/null 2>&1; "
           f"ls {_RENDER_DIR}/*.png 2>/dev/null | head -1")             # echo the PNG path
    try:
        out = vm.run_command(cmd, timeout=_RENDER_TIMEOUT)
    except Exception as e:                                 # noqa: BLE001
        return None, f"render failed ({type(e).__name__})"
    png = ""
    for line in (out or "").splitlines():
        line = line.strip()
        if line.endswith(".png"):
            png = line
            break
    if not png:
        return None, f"could not render {path} to an image (no output produced)"
    return vm.fetch_file(png)


def prepare_look_images(datas: list, region=None, max_side: int = None):
    """``datas``: list of raw image bytes. Returns (attachments, note) where
    ``attachments`` preserves every requested look item, in input order, and ``note``
    describes that order for the model. A single large image receives overview + zoom
    tiles. A multi-image comparison receives one bounded full view per item so tiles
    cannot change image numbering or crowd later requested paths out of the call.
    Pass-through (with empty note) when PIL is missing."""
    try:
        from PIL import Image
    except ImportError:                                    # graceful degrade
        return datas, ""
    out, parts = [], []
    multi = len(datas) > 1
    for k, data in enumerate(datas):
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:                                  # noqa: BLE001 — not an
            out.append(data)                               # image PIL groks: attach
            parts.append(f"image {k + 1} as given")        # raw, let the API judge
            continue
        if region and len(region) == 4:
            try:
                x0, y0, x1, y1 = (int(v) for v in region)
                img = img.crop((max(0, x0), max(0, y0),
                                min(img.width, x1), min(img.height, y1)))
                out.append(_encode(_bounded(img, max_side or MAX_SIDE)))
                parts.append(f"image {k + 1}: requested region ({x0},{y0})-({x1},{y1})")
                continue                                   # a crop IS the zoom —
            except Exception:                              # noqa: BLE001
                pass                                       # bad box -> fall through
        w, h = img.size
        if max(w, h) <= TILE_THRESHOLD or multi:
            out.append(_encode(_bounded(img, max_side or MAX_SIDE)))
            suffix = " (downscaled)" if max(w, h) > (max_side or MAX_SIDE) else ""
            parts.append(f"image {k + 1}: full view{suffix}")
            continue
        out.append(_encode(_bounded(img, max_side or MAX_SIDE)))   # overview first
        names = ("top-left", "top-right", "bottom-left", "bottom-right")
        ow, oh = int(w * OVERLAP), int(h * OVERLAP)
        boxes = [(0, 0, w // 2 + ow, h // 2 + oh),
                 (w // 2 - ow, 0, w, h // 2 + oh),
                 (0, h // 2 - oh, w // 2 + ow, h),
                 (w // 2 - ow, h // 2 - oh, w, h)]
        added = []
        for name, box in zip(names, boxes):
            out.append(_encode(_bounded(img.crop(box))))
            added.append(name)
        parts.append(f"image {k + 1}: full view (downscaled), then native-resolution "
                     f"tiles ({', '.join(added)}, {int(OVERLAP * 100)}% overlap)")
    note = "Attached: " + "; ".join(parts) + "." if parts else ""
    # v34: `parts` is one entry per LOOK ITEM (the tile path adds 5 attachments
    # under a single entry), so zip(out, parts) mislabeled and DROPPED archives.
    # Archive with a parallel per-attachment label list instead.
    labels = list(parts)
    while len(labels) < len(out):
        labels.append(f"{labels[-1] if labels else 'img'}_tile{len(labels)}")
    for _d, _l in zip(out, labels):                        # v32.5/v34: eyes archive
        _archive(_d, _l)
    return out, note
