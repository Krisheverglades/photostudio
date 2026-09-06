"""
Photo processing pipeline.

Three jobs, mirroring what Aftershoot/Imagen do under the hood:
  1. score_image()   -> technical quality score (sharpness, exposure, faces/eyes)
  2. auto_edit()      -> crop + tone/color correction
  3. select_best()    -> rank a shoot and keep the top N

This is a solid, inspectable starting point. The scoring model here is
heuristic (classic computer vision), not a learned aesthetic model — it's
meant to be a working baseline you can later replace with a fine-tuned
model trained on photos you've personally picked in the past.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

# Haar cascades ship with opencv-python-headless; used for a simple,
# dependency-light face/eye check. Swap for a proper face-detection
# model (e.g. MediaPipe or a YOLO face model) for better accuracy.
_FACE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)
_EYE_CASCADE = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_eye.xml"
)


@dataclass
class ImageScore:
    path: str
    sharpness: float = 0.0
    exposure: float = 0.0
    face_score: float = 0.0
    duplicate_group: int | None = None
    total: float = 0.0
    flags: list[str] = field(default_factory=list)


def _sharpness_score(gray: np.ndarray) -> float:
    """Variance of the Laplacian — higher = crisper focus."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _exposure_score(gray: np.ndarray) -> float:
    """
    Penalize crushed blacks / blown highlights.
    Returns 0-100, where 100 = well-balanced histogram.
    """
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    hist /= hist.sum() + 1e-8
    shadows_clipped = hist[:5].sum()
    highlights_clipped = hist[-5:].sum()
    penalty = (shadows_clipped + highlights_clipped) * 100
    return max(0.0, 100.0 - penalty * 4)


def _face_eye_score(gray: np.ndarray) -> tuple[float, list[str]]:
    """Rewards open eyes on detected faces; flags closed-eye shots."""
    flags: list[str] = []
    faces = _FACE_CASCADE.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
    if len(faces) == 0:
        return 50.0, flags  # neutral score for non-portrait shots (landscapes, etc.)

    score = 0.0
    for (x, y, w, h) in faces:
        face_roi = gray[y : y + h, x : x + w]
        eyes = _EYE_CASCADE.detectMultiScale(face_roi, 1.1, 5, minSize=(15, 15))
        if len(eyes) >= 2:
            score += 100
        elif len(eyes) == 1:
            score += 60
            flags.append("possible partial eye closure")
        else:
            score += 20
            flags.append("possible closed eyes")
    return score / len(faces), flags


def score_image(path: str) -> ImageScore:
    img = cv2.imread(path)
    if img is None:
        return ImageScore(path=path, flags=["unreadable file"])

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharp = _sharpness_score(gray)
    expo = _exposure_score(gray)
    face_score, flags = _face_eye_score(gray)

    if sharp < 40:
        flags.append("likely blurry")

    # Weighted composite: sharpness matters most, then faces/eyes, then exposure.
    # Sharpness is unbounded (Laplacian variance), so compress it with a log scale.
    sharp_norm = min(100.0, np.log1p(sharp) * 12)
    total = 0.45 * sharp_norm + 0.35 * face_score + 0.20 * expo

    return ImageScore(
        path=path,
        sharpness=sharp_norm,
        exposure=expo,
        face_score=face_score,
        total=round(total, 2),
        flags=flags,
    )


def find_duplicate_groups(scores: list[ImageScore], hash_threshold: int = 6) -> None:
    """
    Groups visually-similar consecutive shots (burst sequences) using a
    cheap perceptual hash, so select_best() only keeps the top shot per burst.
    Mutates `duplicate_group` on each ImageScore in place.
    """
    def phash(path: str) -> np.ndarray:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        img = cv2.resize(img, (16, 16), interpolation=cv2.INTER_AREA)
        dct = cv2.dct(np.float32(img))
        low_freq = dct[:8, :8].flatten()
        median = np.median(low_freq)
        return (low_freq > median).astype(np.uint8)

    hashes = [phash(s.path) for s in scores]
    group_id = 0
    assigned = [False] * len(scores)

    for i in range(len(scores)):
        if assigned[i]:
            continue
        scores[i].duplicate_group = group_id
        assigned[i] = True
        for j in range(i + 1, len(scores)):
            if assigned[j]:
                continue
            dist = int(np.count_nonzero(hashes[i] != hashes[j]))
            if dist <= hash_threshold:
                scores[j].duplicate_group = group_id
                assigned[j] = True
        group_id += 1


def select_best(scores: list[ImageScore], count: int = 100) -> list[ImageScore]:
    """
    Picks the top `count` images, but never more than one per duplicate
    (burst) group unless the shoot is smaller than `count` groups.
    """
    find_duplicate_groups(scores)
    by_group: dict[int, list[ImageScore]] = {}
    for s in scores:
        by_group.setdefault(s.duplicate_group, []).append(s)

    # Best representative of each burst
    representatives = [max(group, key=lambda s: s.total) for group in by_group.values()]
    representatives.sort(key=lambda s: s.total, reverse=True)

    if len(representatives) >= count:
        return representatives[:count]

    # Not enough distinct bursts to hit `count` — top up with next-best
    # remaining shots (still sorted by score) until we reach `count` or run out.
    chosen_paths = {r.path for r in representatives}
    remainder = sorted(
        [s for s in scores if s.path not in chosen_paths],
        key=lambda s: s.total,
        reverse=True,
    )
    return (representatives + remainder)[:count]


def auto_edit(
    src_path: str,
    dst_path: str,
    target_aspect: float | None = None,
    style: str = "natural",
) -> None:
    """
    Applies an automatic crop + tone/color pass.

    style: "natural" | "warm" | "moody" | "bright_airy"
      Swap this for a per-client "learned style" once you have a
      trained model — this is the deterministic baseline.
    """
    img = Image.open(src_path)
    source_format = img.format
    img = ImageOps.exif_transpose(img)  # respect camera orientation

    if target_aspect:
        img = _smart_crop(img, target_aspect)

    img = _auto_levels(img)

    presets = {
        "natural": dict(color=1.05, contrast=1.05, brightness=1.02, warmth=0),
        "warm": dict(color=1.10, contrast=1.05, brightness=1.03, warmth=8),
        "moody": dict(color=0.92, contrast=1.15, brightness=0.95, warmth=-4),
        "bright_airy": dict(color=1.02, contrast=0.95, brightness=1.12, warmth=4),
    }
    p = presets.get(style, presets["natural"])

    img = ImageEnhance.Color(img).enhance(p["color"])
    img = ImageEnhance.Contrast(img).enhance(p["contrast"])
    img = ImageEnhance.Brightness(img).enhance(p["brightness"])
    if p["warmth"]:
        img = _adjust_warmth(img, p["warmth"])

    img = ImageEnhance.Sharpness(img).enhance(1.15)

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    save_kwargs = {}
    if source_format in {"JPEG", "JPG"}:
        save_kwargs = {"quality": 100, "subsampling": 0}
    img.save(dst_path, **save_kwargs)


def _smart_crop(img: Image.Image, target_aspect: float) -> Image.Image:
    """Center-weighted crop to target aspect ratio (falls back to face-centering if a face is found)."""
    w, h = img.size
    current_aspect = w / h

    if abs(current_aspect - target_aspect) < 0.01:
        return img

    cv_img = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    faces = _FACE_CASCADE.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
    if len(faces) > 0:
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        cx, cy = x + fw / 2, y + fh / 2
    else:
        cx, cy = w / 2, h / 2

    if current_aspect > target_aspect:
        new_w = int(h * target_aspect)
        left = max(0, min(w - new_w, int(cx - new_w / 2)))
        return img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_aspect)
        top = max(0, min(h - new_h, int(cy - new_h / 2)))
        return img.crop((0, top, w, top + new_h))


def _auto_levels(img: Image.Image) -> Image.Image:
    """Simple per-channel histogram stretch — a lightweight auto-white-balance/contrast pass."""
    arr = np.array(img.convert("RGB")).astype(np.float32)
    for c in range(3):
        channel = arr[:, :, c]
        lo, hi = np.percentile(channel, (1, 99))
        if hi > lo:
            arr[:, :, c] = np.clip((channel - lo) * 255.0 / (hi - lo), 0, 255)
    return Image.fromarray(arr.astype(np.uint8))


def _adjust_warmth(img: Image.Image, amount: int) -> Image.Image:
    arr = np.array(img.convert("RGB")).astype(np.int16)
    arr[:, :, 0] = np.clip(arr[:, :, 0] + amount, 0, 255)   # red
    arr[:, :, 2] = np.clip(arr[:, :, 2] - amount, 0, 255)   # blue
    return Image.fromarray(arr.astype(np.uint8))


def process_shoot(
    input_dir: str,
    output_dir: str,
    best_count: int = 100,
    target_aspect: float | None = None,
    style: str = "natural",
) -> list[ImageScore]:
    """
    Full pipeline for one client shoot:
      1. score every photo
      2. auto-crop + auto-edit every photo into output_dir/all/
      3. select the top `best_count` and copy them into output_dir/best/
    Returns the ranked scores for the whole shoot.
    """
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
    files = [
        os.path.join(input_dir, f)
        for f in sorted(os.listdir(input_dir))
        if os.path.splitext(f)[1].lower() in exts
    ]

    scores = [score_image(f) for f in files]

    all_dir = os.path.join(output_dir, "all")
    best_dir = os.path.join(output_dir, "best")
    os.makedirs(all_dir, exist_ok=True)
    os.makedirs(best_dir, exist_ok=True)

    for s in scores:
        fname = os.path.basename(s.path)
        auto_edit(s.path, os.path.join(all_dir, fname), target_aspect, style)

    best = select_best(scores, count=best_count)
    for s in best:
        fname = os.path.basename(s.path)
        edited_path = os.path.join(all_dir, fname)
        if os.path.exists(edited_path):
            shutil.copy2(edited_path, os.path.join(best_dir, fname))

    scores.sort(key=lambda s: s.total, reverse=True)
    return scores
