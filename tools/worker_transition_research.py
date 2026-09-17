#!/usr/bin/env python3
"""Find and visually sample YouTube videos likely to show worker clothing/boot transitions.

Pipeline:
1. Search YouTube with ytscrape using occupation-native transition queries.
2. Download human/auto English captions with yt-dlp and locate likely action timestamps.
3. Download only short video sections around those timestamps.
4. Extract still frames for later visual action coding.

A caption hit is only a locator. It is NOT treated as proof that the action is visible.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OUT = Path("research_output")
OUT.mkdir(exist_ok=True)

QUERIES = [
    "construction vlog change work boots truck after work",
    "construction worker change into Crocs after work",
    "construction parking lot change shoes after shift",
    "concrete worker muddy boots truck vlog",
    "concrete pour end of day change boots",
    "heavy equipment operator boots off cab",
    "excavator operator Crocs cab boots off",
    "equipment operator change shoes cab",
    "lineman change wet clothes truck",
    "lineman storm work dry clothes truck",
    "diesel field mechanic wet clothes service truck",
    "mobile mechanic muddy boots service truck",
    "work boots off before driving home construction",
    "jobsite change shoes at truck",
    "end of shift work clothes truck construction vlog",
]

TITLE_NOISE = re.compile(
    r"\b(review|best work boots?|boot review|footwear review|unboxing|commercial|official music|anthem|how to choose|buying guide)\b",
    re.I,
)

# Caption phrases used only to locate candidate timestamps.
PATTERNS = [
    r"change(?:d|ing)? (?:my |these |out of )?(?:boots|shoes|clothes|shirt|pants|bibs|coveralls)",
    r"change out of (?:my |these )?(?:boots|shoes|clothes|bibs|coveralls)",
    r"take (?:my |these )?(?:boots|shoes|clothes|bibs|coveralls) off",
    r"take off (?:my |these )?(?:boots|shoes|clothes|bibs|coveralls)",
    r"boots? (?:are |is )?off",
    r"shoes? (?:are |is )?off",
    r"change into (?:crocs|slides|shoes|sneakers|dry clothes|clean clothes)",
    r"put on (?:my )?(?:crocs|slides|shoes|sneakers|boots)",
    r"dry clothes",
    r"wet clothes",
    r"clean clothes",
    r"change of clothes",
    r"drive home",
    r"head(?:ing)? home",
    r"go(?:ing)? home",
    r"end of (?:the )?(?:day|shift)",
    r"parking lot",
    r"back at (?:my |the )?truck",
    r"at (?:my |the )?truck",
    r"in (?:my |the )?truck",
    r"inside (?:my |the )?cab",
    r"keep (?:my |the )?cab clean",
    r"muddy boots",
    r"dirty boots",
]
RX = re.compile("|".join(f"(?:{p})" for p in PATTERNS), re.I)

VTT_TS = re.compile(
    r"(?P<h1>\d{2}):(?P<m1>\d{2}):(?P<s1>\d{2}(?:\.\d+)?)\s+-->\s+"
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2}(?:\.\d+)?)"
)
TAG_RX = re.compile(r"<[^>]+>")


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
            "search", query, "--filter", "videos", "--max", "18", "--format", "json",
        ])
        for item in parse_search_json(cp.stdout):
            vid = item.get("video_id") or item.get("id")
            if not vid:
                url = item.get("url", "")
                m = re.search(r"[?&]v=([\w-]{11})", url)
                vid = m.group(1) if m else None
            if not vid:
                continue
            title = str(item.get("title") or "")
            if TITLE_NOISE.search(title):
                continue
            item["_query"] = query
            seen.setdefault(str(vid), item)
    rows = list(seen.values())
    (OUT / "search_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows


def ts_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def clean_vtt_text(text: str) -> str:
    text = html.unescape(TAG_RX.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    cues: list[tuple[float, str]] = []
    i = 0
    while i < len(lines):
        m = VTT_TS.search(lines[i])
        if not m:
            i += 1
            continue
        start = ts_seconds(m.group("h1"), m.group("m1"), m.group("s1"))
        i += 1
        text_parts: list[str] = []
        while i < len(lines) and lines[i].strip() != "":
            line = lines[i].strip()
            if "-->" not in line and not line.startswith("NOTE"):
                text_parts.append(line)
            i += 1
        text = clean_vtt_text(" ".join(text_parts))
        if text:
            cues.append((start, text))
        i += 1
    return cues


def download_subtitles(item: dict[str, Any], subdir: Path) -> tuple[list[tuple[float, str]], str]:
    vid = str(item.get("video_id") or item.get("id") or "")
    url = str(item.get("url") or f"https://www.youtube.com/watch?v={vid}")
    outtmpl = str(subdir / f"{vid}.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
        "--skip-download", "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*,en", "--sub-format", "vtt",
        "-o", outtmpl, url,
    ]
    try:
        cp = run(cmd, timeout=180, check=False)
    except Exception as exc:
        return [], str(exc)

    candidates = sorted(subdir.glob(f"{vid}*.vtt"))
    if not candidates:
        err = cp.stderr[-3000:] if cp.stderr else "no subtitle file produced"
        return [], err

    # Prefer plain English tracks over translated tracks when several exist.
    candidates.sort(key=lambda p: (".en.vtt" not in p.name, len(p.name)))
    try:
        return parse_vtt(candidates[0]), ""
    except Exception as exc:
        return [], f"parse_vtt: {exc}"


def locate_hits(rows: list[dict[str, Any]], max_transcripts: int = 140) -> list[Hit]:
    hits: list[Hit] = []
    transcript_dir = OUT / "transcripts"
    transcript_dir.mkdir(exist_ok=True)
    errors: list[dict[str, str]] = []
    captioned = 0

    for item in rows[:max_transcripts]:
        vid = str(item.get("video_id") or item.get("id") or "")
        if not vid:
            continue
        cues, err = download_subtitles(item, transcript_dir)
        if not cues:
            errors.append({"video_id": vid, "title": str(item.get("title") or ""), "error": err})
            continue
        captioned += 1

        title = str(item.get("title") or "")
        channel = str(item.get("channel") or item.get("channel_name") or "")
        url = str(item.get("url") or f"https://www.youtube.com/watch?v={vid}")
        query = str(item.get("_query") or "")

        per_video = 0
        last_ts = -9999.0
        for ts, text in cues:
            if RX.search(text):
                # Collapse repeated auto-caption cues around the same spoken phrase.
                if ts - last_ts < 8:
                    continue
                hits.append(Hit(vid, title, channel, url, query, ts, text))
                last_ts = ts
                per_video += 1
                if per_video >= 5:
                    break

    (OUT / "subtitle_errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "caption_stats.json").write_text(
        json.dumps({"attempted": min(len(rows), max_transcripts), "captioned_videos": captioned, "failed": len(errors)}, indent=2),
        encoding="utf-8",
    )
    (OUT / "hits.json").write_text(
        json.dumps([asdict(h) for h in hits], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return hits


def hhmmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def extract_visuals(hits: list[Hit], max_segments: int = 16) -> list[dict[str, Any]]:
    visual_dir = OUT / "visual_candidates"
    visual_dir.mkdir(exist_ok=True)
    manifest: list[dict[str, Any]] = []
    used: set[tuple[str, int]] = set()

    # Prioritize direct transition language before generic truck/cab mentions.
    direct = re.compile(r"boots? off|shoes? off|change|crocs|slides|dry clothes|wet clothes|clean clothes|muddy boots|dirty boots", re.I)
    ordered_hits = sorted(hits, key=lambda h: (0 if direct.search(h.text) else 1, h.video_id, h.timestamp))

    for hit in ordered_hits:
        bucket = int(hit.timestamp // 45)
        key = (hit.video_id, bucket)
        if key in used:
            continue
        used.add(key)
        if len(manifest) >= max_segments:
            break

        start = max(0, hit.timestamp - 25)
        end = hit.timestamp + 45
        folder = visual_dir / f"{hit.video_id}_{int(hit.timestamp)}"
        folder.mkdir(exist_ok=True)
        outtmpl = str(folder / "clip.%(ext)s")

        ytdlp = [
            "yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
            "--download-sections", f"*{hhmmss(start)}-{hhmmss(end)}",
            "--force-keyframes-at-cuts",
            "-f", "bv*[height<=480]+ba/b[height<=480]/worst",
            "--merge-output-format", "mp4",
            "-o", outtmpl,
            hit.url,
        ]
        ok = False
        error = ""
        clip: Path | None = None
        try:
            cp = run(ytdlp, timeout=300, check=False)
            files = [p for p in folder.glob("clip.*") if p.suffix.lower() in {".mp4", ".webm", ".mkv"}]
            clip = files[0] if files else None
            ok = clip is not None and clip.exists()
            if not ok:
                error = cp.stderr[-3000:] if cp.stderr else "no clip file produced"
        except Exception as exc:
            error = str(exc)

        frames: list[str] = []
        if ok and clip is not None:
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
        rec.update({
            "segment_start": start,
            "segment_end": end,
            "download_ok": ok,
            "clip": str(clip) if clip else "",
            "frames": frames,
            "error": error,
        })
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
        "frames_extracted": sum(len(x["frames"]) for x in manifest),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
