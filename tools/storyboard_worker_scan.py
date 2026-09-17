#!/usr/bin/env python3
"""Download YouTube storyboard sprite sheets for visual worker-transition research.

This deliberately avoids yt-dlp video/caption downloads. It uses ytscrape's
InnerTube player response to obtain YouTube's own storyboard preview frames,
then downloads the sprite sheets from i.ytimg.com for later visual coding.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from ytscrape import SearchFilter, YouTube

OUT = Path("storyboard_output")
OUT.mkdir(exist_ok=True)
SHEETS = OUT / "sheets"
SHEETS.mkdir(exist_ok=True)

QUERIES = [
    "Victory Outdoor Services end of day concrete vlog",
    "Victory Outdoor Services mud rain concrete",
    "Dirt Perfect excavator cab work vlog",
    "LetsDig18 excavator operator cab vlog",
    "heavy equipment operator boots off cab",
    "excavator operator Crocs cab",
    "construction worker after work truck vlog",
    "construction end of day parking lot work boots",
    "concrete worker muddy boots truck vlog",
    "lineman storm work wet clothes truck vlog",
    "Bobsdecline lineman day in the life",
    "lineman day in the life truck yard",
    "TekamoHD field mechanic day in life service truck",
    "Western Truck and Tractor Repair day in life",
    "mobile diesel mechanic service truck vlog",
]

NOISE = re.compile(
    r"\b(review|best work boots?|buying guide|unboxing|official music|kids|cartoon|draw|how to install|shoes i wear)\b",
    re.I,
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"


def add_query(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = value
    return urlunparse(p._replace(query=urlencode(q)))


def parse_storyboard(spec: str, length_seconds: int | None):
    parts = spec.split("|")
    if len(parts) < 2:
        return []
    base = parts[0]
    levels = []
    for idx, raw in enumerate(parts[1:]):
        bits = raw.split("#")
        if len(bits) < 8:
            continue
        try:
            width, height, count, cols, rows, interval = map(int, bits[:6])
        except ValueError:
            continue
        name, sigh = bits[6], bits[7]
        if interval == 0 and length_seconds and count:
            interval = max(1, int(length_seconds * 1000 / count))
        url = base.replace("$L", str(idx)).replace("$N", name)
        url = add_query(url, "sigh", sigh)
        images_count = max(1, math.ceil(count / max(1, cols * rows)))
        levels.append({
            "level": idx, "width": width, "height": height, "count": count,
            "columns": cols, "rows": rows, "interval_ms": interval,
            "name": name, "url": url, "images_count": images_count,
        })
    return levels


def main() -> None:
    yt = YouTube(region="US")
    seen = {}
    query_for = {}
    for query in QUERIES:
        try:
            for item in yt.search(query, filter=SearchFilter.VIDEOS, max_results=12):
                title = (item.title or "").strip()
                if not item.video_id or NOISE.search(title):
                    continue
                seen.setdefault(item.video_id, {
                    "video_id": item.video_id,
                    "title": title,
                    "channel": item.channel or "",
                    "url": item.url,
                    "duration": item.duration or "",
                    "published": item.published or "",
                })
                query_for.setdefault(item.video_id, query)
        except Exception:
            continue

    candidates = list(seen.values())[:70]
    (OUT / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = []
    errors = []
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://www.youtube.com/"})

    successful_videos = 0
    downloaded_sheets = 0
    for item in candidates:
        if successful_videos >= 45:
            break
        vid = item["video_id"]
        try:
            player = yt.client.player(vid, client_name="WEB")
            details = player.get("videoDetails") or {}
            try:
                length = int(details.get("lengthSeconds") or 0)
            except (TypeError, ValueError):
                length = 0
            renderer = ((player.get("storyboards") or {}).get("playerStoryboardSpecRenderer") or {})
            spec = renderer.get("spec")
            if not isinstance(spec, str) or not spec:
                errors.append({"video_id": vid, "title": item["title"], "error": "no storyboard spec"})
                continue
            levels = parse_storyboard(spec, length)
            if not levels:
                errors.append({"video_id": vid, "title": item["title"], "error": "storyboard parse failed"})
                continue
            # Highest-resolution level is generally last.
            level = levels[-1]
            folder = SHEETS / vid
            folder.mkdir(exist_ok=True)
            max_sheets = min(level["images_count"], 16)
            saved = []
            sheet_interval = level["interval_ms"] * level["columns"] * level["rows"]
            for n in range(max_sheets):
                url = level["url"].replace("$M", str(n))
                r = session.get(url, timeout=30)
                if r.status_code != 200 or not r.content:
                    continue
                path = folder / f"sheet_{n:03d}.jpg"
                path.write_bytes(r.content)
                saved.append({
                    "path": str(path),
                    "sheet_index": n,
                    "approx_start_seconds": round((n * sheet_interval) / 1000, 1),
                    "approx_end_seconds": round(((n + 1) * sheet_interval) / 1000, 1),
                })
                downloaded_sheets += 1
            if not saved:
                errors.append({"video_id": vid, "title": item["title"], "error": "storyboard image download failed"})
                continue
            successful_videos += 1
            manifest.append({
                **item,
                "query": query_for.get(vid, ""),
                "length_seconds": length,
                "storyboard": {k: v for k, v in level.items() if k != "url"},
                "sheets": saved,
            })
        except Exception as exc:
            errors.append({"video_id": vid, "title": item["title"], "error": str(exc)})

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "queries": len(QUERIES),
        "unique_candidates": len(candidates),
        "storyboard_videos": successful_videos,
        "storyboard_sheets": downloaded_sheets,
        "errors": len(errors),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
