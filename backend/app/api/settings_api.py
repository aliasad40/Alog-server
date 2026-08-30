"""Branding and retention settings."""

from __future__ import annotations

import logging
import secrets
from io import BytesIO
from pathlib import Path

from fastapi import (APIRouter, File, HTTPException, Request, UploadFile,
                     status)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .deps import CurrentUser, SameOrigin, get_cfg, get_ch, get_meta

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_PIXELS = 4000


class Branding(BaseModel):
    company_name: str = Field(min_length=1, max_length=64)


class Retention(BaseModel):
    hot_days: int = Field(ge=1, le=3650)
    retention_months: int = Field(ge=1, le=120)


@router.get("/branding")
async def get_branding(request: Request):
    """Unauthenticated: the login page needs the company name and logo
    before anyone has signed in. It exposes nothing an anonymous visitor
    could not already see on the login screen."""
    meta = get_meta(request)
    logo = meta.get_setting("logo_filename")
    return {
        "company_name": meta.get_setting("company_name", "Network Log Server"),
        "logo_url": f"/api/settings/logo/{logo}" if logo else None,
    }


@router.put("/branding")
async def set_branding(payload: Branding, request: Request,
                       _user: str = CurrentUser, _: None = SameOrigin):
    get_meta(request).set_setting("company_name", payload.company_name.strip())
    return {"status": "Branding updated"}


def _process_logo(raw: bytes, target_dir: Path) -> str:
    """Re-encode the upload to a clean PNG.

    Never trust an uploaded image. Decoding it and writing a fresh PNG strips
    EXIF, embedded scripts and polyglot payloads: whatever we serve back is
    something Pillow generated, not something a user supplied. SVG is refused
    outright because it is an XSS vector by design.
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_LOGO_PIXELS * MAX_LOGO_PIXELS
    try:
        with Image.open(BytesIO(raw)) as img:
            img.verify()
        with Image.open(BytesIO(raw)) as img:
            if img.format not in ("PNG", "JPEG", "GIF", "WEBP", "BMP"):
                raise ValueError(f"{img.format} images are not accepted")
            if img.width > MAX_LOGO_PIXELS or img.height > MAX_LOGO_PIXELS:
                raise ValueError("Image is too large; use 4000x4000 pixels or smaller")
            img = img.convert("RGBA")
            img.thumbnail((512, 512))
            filename = f"logo-{secrets.token_hex(8)}.png"
            target_dir.mkdir(parents=True, exist_ok=True)
            out = target_dir / filename
            img.save(out, format="PNG", optimize=True)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("That file is not a readable image.") from exc
    return filename


@router.post("/logo")
async def upload_logo(request: Request, file: UploadFile = File(...),
                      _user: str = CurrentUser, _: None = SameOrigin):
    raw = await file.read(MAX_LOGO_BYTES + 1)
    if len(raw) > MAX_LOGO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "Logo must be 2 MB or smaller.")
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file was empty.")

    cfg = get_cfg(request)
    meta = get_meta(request)
    logo_dir = Path(cfg.paths.logo_dir)
    try:
        filename = await run_in_threadpool(_process_logo, raw, logo_dir)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    previous = meta.get_setting("logo_filename")
    meta.set_setting("logo_filename", filename)
    if previous:
        try:
            (logo_dir / previous).unlink(missing_ok=True)
        except OSError:
            pass
    return {"status": "Logo updated", "logo_url": f"/api/settings/logo/{filename}"}


@router.delete("/logo")
async def clear_logo(request: Request, _user: str = CurrentUser, _: None = SameOrigin):
    meta = get_meta(request)
    previous = meta.get_setting("logo_filename")
    meta.set_setting("logo_filename", "")
    if previous:
        try:
            (Path(get_cfg(request).paths.logo_dir) / previous).unlink(missing_ok=True)
        except OSError:
            pass
    return {"status": "Logo removed"}


@router.get("/logo/{filename}")
async def serve_logo(filename: str, request: Request):
    from fastapi.responses import FileResponse

    # The filename comes from our own generator, but validate anyway: a path
    # traversal here would serve arbitrary files to anonymous visitors.
    if not filename.startswith("logo-") or not filename.endswith(".png") or "/" in filename:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such logo.")
    path = Path(get_cfg(request).paths.logo_dir) / filename
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such logo.")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=300"})


@router.get("/retention")
async def get_retention(request: Request, _user: str = CurrentUser):
    meta = get_meta(request)
    return {
        "hot_days": int(meta.get_setting("hot_days", "30")),
        "retention_months": int(meta.get_setting("retention_months", "12")),
    }


@router.put("/retention")
async def set_retention(payload: Retention, request: Request,
                        _user: str = CurrentUser, _: None = SameOrigin):
    service = get_ch(request)
    try:
        await run_in_threadpool(service.apply_retention, payload.hot_days,
                                payload.retention_months)
    except Exception as exc:
        log.error("could not apply TTL: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "ClickHouse refused the retention change. Nothing was modified.")
    meta = get_meta(request)
    meta.set_setting("hot_days", str(payload.hot_days))
    meta.set_setting("retention_months", str(payload.retention_months))
    log.info("retention set: hot=%dd retain=%dmo", payload.hot_days, payload.retention_months)
    return {"status": "Retention updated"}
