#!/usr/bin/env python3
"""
YouTube Football Niche Scraper — powered by Apify
Collects videos metadata, comments, and transcripts from the last N days.
Output: CSV files in ./output/
"""

import os
import csv
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from apify_client import ApifyClient

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
APIFY_TOKEN = os.getenv("APIFY_TOKEN")

KEYWORDS = [
    "futebol",
    "gols hoje",
    "campeonato brasileiro",
    "copa do brasil",
    "futebol ao vivo",
    "melhores momentos futebol",
]

DAYS_BACK = 15
MAX_VIDEOS_PER_KEYWORD = 50   # increased from 30
MAX_COMMENTS_PER_VIDEO = 500  # increased from 200
COMMENTS_BATCH_SIZE    = 20   # video URLs per actor run (avoids timeouts)
OUTPUT_DIR = Path("output")

ACTOR_YOUTUBE    = "streamers/youtube-scraper"
ACTOR_TRANSCRIPT = "pintostudio/youtube-transcript-scraper"

CUTOFF_DATE = datetime.now(tz=timezone.utc) - timedelta(days=DAYS_BACK)
SCRAPED_AT  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Text & date helpers ────────────────────────────────────────────────────────
def clean_text(text: str | None) -> str:
    """Normalize unicode, strip null bytes, collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\x00", "")                    # null bytes
    text = re.sub(r"[\r\n\t]+", " ", text)             # newlines / tabs → space
    text = re.sub(r" {2,}", " ", text)                 # collapse multiple spaces
    return text.strip()


_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
)

def normalize_date(date_str: str | None) -> str:
    """Parse any known ISO-ish format and return YYYY-MM-DDTHH:MM:SSZ, or original string."""
    if not date_str:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return date_str  # keep as-is if unparseable (e.g. "3 days ago")


def within_window(date_str: str | None) -> bool:
    if not date_str:
        return True
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= CUTOFF_DATE
        except ValueError:
            continue
    return True  # keep if unparseable


# ── CSV writer ─────────────────────────────────────────────────────────────────
def save_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        print(f"  [!] No data to save for {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"  [+] {len(rows):>5} rows  →  {path}")


def batched(lst: list, size: int):
    """Yield successive chunks of `size` from `lst`."""
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ── Step 1: Videos + Metadata ──────────────────────────────────────────────────
def scrape_videos(client: ApifyClient) -> list[dict]:
    all_videos: list[dict] = []
    seen_ids: set[str] = set()

    for keyword in KEYWORDS:
        print(f"  → keyword: '{keyword}'")
        run = client.actor(ACTOR_YOUTUBE).call(
            run_input={
                "searchQueries": [keyword],
                "maxResults":    MAX_VIDEOS_PER_KEYWORD,
                "dateFilter":    "month",        # Apify pre-filter: last 30 days
                "sort":          "upload_date",  # newest first
                "maxComments":   0,
                "proxyConfiguration": {"useApifyProxy": True},
            }
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

        batch_count = 0
        for item in items:
            video_id = item.get("id")
            if not video_id or video_id in seen_ids:
                continue

            pub_raw = item.get("date") or item.get("uploadDate") or ""
            if not within_window(pub_raw):
                continue

            seen_ids.add(video_id)
            batch_count += 1
            all_videos.append({
                "video_id":      video_id,
                "title":         clean_text(item.get("title")),
                "url":           item.get("url", f"https://www.youtube.com/watch?v={video_id}"),
                "channel_name":  clean_text(item.get("channelName")),
                "channel_id":    item.get("channelId", ""),
                "published_at":  normalize_date(pub_raw),
                "duration":      item.get("duration", ""),
                "view_count":    item.get("viewCount", ""),
                "like_count":    item.get("likes", ""),
                "comment_count": item.get("commentsCount", ""),
                "description":   clean_text((item.get("description") or "")[:500]),
                "keyword":       keyword,
                "scraped_at":    SCRAPED_AT,
            })

        print(f"     {batch_count} valid (total unique: {len(all_videos)})")
        time.sleep(1)

    return all_videos


# ── Step 2: Comments ───────────────────────────────────────────────────────────
def scrape_comments(client: ApifyClient, video_urls: list[str]) -> list[dict]:
    if not video_urls:
        return []

    all_rows: list[dict] = []
    seen_comment_ids: set[str] = set()
    batches = list(batched(video_urls, COMMENTS_BATCH_SIZE))

    print(f"  → {len(video_urls)} videos split into {len(batches)} batches")

    for i, batch in enumerate(batches, 1):
        print(f"     batch {i}/{len(batches)} ({len(batch)} videos)...")
        run = client.actor(ACTOR_YOUTUBE).call(
            run_input={
                "startUrls":   [{"url": url} for url in batch],
                "maxResults":  1,
                "maxComments": MAX_COMMENTS_PER_VIDEO,
                "proxyConfiguration": {"useApifyProxy": True},
            }
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

        for item in items:
            video_id  = item.get("id", "")
            video_url = item.get("url", "")
            for comment in item.get("comments", []):
                cid = comment.get("id", "")
                if cid and cid in seen_comment_ids:
                    continue
                if cid:
                    seen_comment_ids.add(cid)

                all_rows.append({
                    "video_id":     video_id,
                    "video_url":    video_url,
                    "comment_id":   cid,
                    "author":       clean_text(comment.get("authorText")),
                    "text":         clean_text(comment.get("text")),
                    "likes":        comment.get("likesCount", 0),
                    "published_at": normalize_date(comment.get("publishedTime")),
                    "is_reply":     comment.get("isReply", False),
                    "reply_to":     comment.get("replyTo", ""),
                    "scraped_at":   SCRAPED_AT,
                })

        time.sleep(1)

    print(f"     {len(all_rows)} unique comments collected")
    return all_rows


# ── Step 3: Transcripts ────────────────────────────────────────────────────────
def scrape_transcripts(
    client: ApifyClient,
    video_urls: list[str],
) -> tuple[list[dict], list[dict]]:
    """
    Returns:
        summary_rows  — one row per video (video_url, language, full_text, segment_count)
        segment_rows  — one row per transcript segment (video_url, start_time, duration, text)
    """
    if not video_urls:
        return [], []

    print(f"  → fetching transcripts for {len(video_urls)} videos")
    run = client.actor(ACTOR_TRANSCRIPT).call(
        run_input={
            "videoUrls": video_urls,
            "language":  "pt",   # falls back to auto-generated if PT unavailable
        }
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    summary_rows: list[dict] = []
    segment_rows: list[dict] = []

    for item in items:
        video_url       = item.get("videoUrl", "")
        language        = item.get("language", "")
        transcript_data = item.get("transcript", [])

        if isinstance(transcript_data, list):
            full_text = clean_text(
                " ".join(seg.get("text", "") for seg in transcript_data)
            )
            for seg in transcript_data:
                segment_rows.append({
                    "video_url":  video_url,
                    "start_time": seg.get("offset") or seg.get("start", ""),
                    "duration":   seg.get("duration", ""),
                    "text":       clean_text(seg.get("text")),
                    "scraped_at": SCRAPED_AT,
                })
        else:
            full_text = clean_text(str(transcript_data))

        summary_rows.append({
            "video_url":     video_url,
            "language":      language,
            "full_text":     full_text,
            "segment_count": len(transcript_data) if isinstance(transcript_data, list) else 0,
            "scraped_at":    SCRAPED_AT,
        })

    print(f"     {len(summary_rows)} transcripts, {len(segment_rows)} segments")
    return summary_rows, segment_rows


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    if not APIFY_TOKEN:
        raise EnvironmentError("APIFY_TOKEN is not set. Add it to your .env file.")

    client = ApifyClient(APIFY_TOKEN)
    OUTPUT_DIR.mkdir(exist_ok=True)

    sep = "=" * 60
    print(sep)
    print("YouTube Football Scraper — Apify")
    print(f"Period  : last {DAYS_BACK} days  (since {CUTOFF_DATE.strftime('%Y-%m-%d')})")
    print(f"Keywords: {len(KEYWORDS)}  |  max {MAX_VIDEOS_PER_KEYWORD} videos each")
    print(f"Comments: up to {MAX_COMMENTS_PER_VIDEO}/video in batches of {COMMENTS_BATCH_SIZE}")
    print(sep)

    # 1. Videos
    print("\n[1/3] Videos & Metadata")
    videos = scrape_videos(client)
    save_csv(videos, OUTPUT_DIR / "videos.csv")

    video_urls = [v["url"] for v in videos if v.get("url")]

    # 2. Comments
    print("\n[2/3] Comments")
    comments = scrape_comments(client, video_urls)
    save_csv(comments, OUTPUT_DIR / "comments.csv")

    # 3. Transcripts
    print("\n[3/3] Transcripts")
    transcripts, segments = scrape_transcripts(client, video_urls)
    save_csv(transcripts, OUTPUT_DIR / "transcripts.csv")
    save_csv(segments,    OUTPUT_DIR / "transcripts_segments.csv")

    print(f"\n{sep}")
    print("Done! Output files:")
    print(f"  videos.csv               — {len(videos):>5} videos")
    print(f"  comments.csv             — {len(comments):>5} comments")
    print(f"  transcripts.csv          — {len(transcripts):>5} transcripts")
    print(f"  transcripts_segments.csv — {len(segments):>5} segments")
    print(sep)


if __name__ == "__main__":
    main()
