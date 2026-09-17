#!/usr/bin/env python3
"""Find YouTube videos likely to show worker clothing/boot transitions.

Pipeline:
1. Search YouTube with ytscrape across several worker-transition queries.
2. Fetch captions and locate phrases around boot/clothing changes.
3. Use yt-dlp + ffmpeg to download short low-res sections around hits.
4. Extract timestamped frames for later visual action coding.

This script is intentionally conservative: a transcript hit is only a locator,
not proof that the visual action is present.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OUT = Path("research_output")
OUT.mkdir(exist_ok=True)

QUERIES = [
    "construction worker change boots after work truck",
    "concrete worker change boots Crocs truck",
    "heavy equipment operator boots off cab Crocs",
    "lineman change wet clothes truck",
    "diesel mechanic change work clothes boots truck",
    "construction end of day change clothes parking lot",
    "work boots off before driving truck construction",
    "muddy boots change shoes at truck jobsite",
]

# Phrase families used only to locate candidate timestamps in captions.
PATTERNS = [
    r"change(?:d|ing)? (?:my |these |out of )?(?:boots|shoes|clothes|shirt|pants|bibs|coveralls)",
    r"take (?:my |these )?(?:boots|shoes|clothes|bibs|coveralls) off",
    r"boots? off",
    r"shoes? off",
    r"change into (?:crocs|slides|shoes|dry clothes|clean clothes)",
    r"put on (?:my )?(?:crocs|slides|shoes|boots)",
    r"dry clothes",
    r"wet clothes",
    r"clean clothes",
    r"drive home",
    r"head(?:ing)? home",
    r"end of (?:the )?(?:day|shift)",
    r"parking lot",
    r"at (?:my |the )?truck",
    r"in (?:my |the )?truck",
    r"cab",
]
RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS), re.I)
TIMED_LINE = re.compile(r"^\[\s*(?P<start>\d+(?:\.\d+)?)\s*\+\s*(?P<dur>\d+(?:\.\d+)?)\]\s*(?P<text>.*)$")


@dataclass
class Hit:
    video_id: str
    title: str
    channel: str
    url: str
    query: str
    timestamp: float
    text: str


def run(cmd: list[str], *, timeout: int = 180, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
    )


def parse_search_json(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "videos"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def search() -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for query in QUERIES:
        cp = run([
            "python", "-m", "ytscrape", "--region", "US",
            "search", query, "--filter", "videos", "--max", "12", "--format", "json",
        ])
        for item in parse_search_json(cp.stdout):
            vid = item.get("video_id") or item.get("id")
            if not vid:
                url = item.get("url", "")
                m = re.search(r"[?&]v=([\w-]{11})", url)
                vid = m.group(1) if m else None
            if not vid:
                continue
            item["_query"] = query
            seen.setdefault(str(vid), item)
    rows = list(seen.values())
    (OUT / "search_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def transcript(video_id: str) -> str | None:
    try:
        cp = run(["python", "-m", "ytscrape", "transcript", video_id, "--lang", "en"], timeout=120)
        return cp.stdout
    except Exception:
        return None


def locate_hits(rows: list[dict[str, Any]], max_transcripts: int = 70) -> list[Hit]:
    hits: list[Hit] = []
    transcript_dir = OUT / "transcripts"
    transcript_dir.mkdir(exist_ok=True)

    for item in rows[:max_transcripts]:
        vid = str(item.get("video_id") or item.get("id") or "")
        if not vid:
            continue
        raw = transcript(vid)
        if not raw:
            continue
        (transcript_dir / f"{vid}.txt").write_text(raw, encoding="utf-8")

        title = str(item.get("title") or "")
        channel = str(item.get("channel") or item.get("channel_name") or "")
        url = str(item.get("url") or f"https://www.youtube.com/watch?v={vid}")
        query = str(item.get("_query") or "")

        per_video = 0
        for line in raw.splitlines():
            m = TIMED_LINE.match(line.strip())
            if not m:
                continue
            text = m.group("text")
            if RX.search(text):
                hits.append(Hit(vid, title, channel, url, query, float(m.group("start")), text))
                per_video += 1
                if per_video >= 4:
                    break

    (OUT / "hits.json").write_text(
        json.dumps([asdict(h) for h in hits], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return hits


def hhmmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def extract_visuals(hits: list[Hit], max_segments: int = 12) -> list[dict[str, Any]]:
    visual_dir = OUT / "visual_candidates"
    visual_dir.mkdir(exist_ok=True)
    manifest: list[dict[str, Any]] = []
    used: set[tuple[str, int]] = set()

    for hit in hits:
        bucket = int(hit.timestamp // 60)
        key = (hit.video_id, bucket)
        if key in used:
            continue
        used.add(key)
        if len(manifest) >= max_segments:
            break

        start = max(0, hit.timestamp - 20)
        end = hit.timestamp + 35
        folder = visual_dir / f"{hit.video_id}_{int(hit.timestamp)}"
        folder.mkdir(exist_ok=True)
        clip = folder / "clip.mp4"

        ytdlp = [
            "yt-dlp",
            "--no-playlist",
            "--quiet",
            "--no-warnings",
            "--download-sections", f"*{hhmmss(start)}-{hhmmss(end)}",
            "-f", "worst[ext=mp4]/worst",
            "-o", str(clip),
            hit.url,
        ]
        ok = False
        error = ""
        try:
            cp = run(ytdlp, timeout=240)
            ok = clip.exists()
            if not ok:
                error = cp.stderr[-2000:]
        except Exception as exc:
            error = str(exc)

        frames: list[str] = []
        if ok:
            frame_pattern = folder / "frame_%03d.jpg"
            try:
                run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(clip), "-vf", "fps=1/2", "-q:v", "3", str(frame_pattern),
                ], timeout=180)
                frames = [str(p) for p in sorted(folder.glob("frame_*.jpg"))]
            except Exception as exc:
                error = f"ffmpeg: {exc}"

        rec = asdict(hit)
        rec.update({"segment_start": start, "segment_end": end, "download_ok": ok, "frames": frames, "error": error})
        manifest.append(rec)

    (OUT / "visual_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    rows = search()
    hits = locate_hits(rows)
    manifest = extract_visuals(hits)
    summary = {
        "search_queries": len(QUERIES),
        "unique_videos": len(rows),
        "transcript_hits": len(hits),
        "visual_segments_attempted": len(manifest),
        "visual_segments_downloaded": sum(1 for x in manifest if x["download_ok"]),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
