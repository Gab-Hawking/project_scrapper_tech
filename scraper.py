#!/usr/bin/env python3
"""
YouTube Football Niche Scraper — powered by Apify
Collects videos metadata, comments, and transcripts from the last N days.
Output: CSV files in ./output/
"""

import os
import csv
import time
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
MAX_VIDEOS_PER_KEYWORD = 30
MAX_COMMENTS_PER_VIDEO = 200
OUTPUT_DIR = Path("output")

# Apify actor IDs
# streamers/youtube-scraper: videos, metadata, comments, transcripts
# See: https://apify.com/streamers/youtube-scraper
ACTOR_YOUTUBE = "streamers/youtube-scraper"

# pintostudio/youtube-transcript-scraper: dedicated transcript extraction
# See: https://apify.com/pintostudio/youtube-transcript-scraper
ACTOR_TRANSCRIPT = "pintostudio/youtube-transcript-scraper"

CUTOFF_DATE = datetime.now(tz=timezone.utc) - timedelta(days=DAYS_BACK)


# ── Helpers ────────────────────────────────────────────────────────────────────
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


def parse_iso_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def within_window(date_str: str | None) -> bool:
    """Returns True if date_str falls within the last DAYS_BACK days."""
    dt = parse_iso_date(date_str)
    if dt is None:
        return True  # keep if unparseable (avoid losing data)
    return dt >= CUTOFF_DATE


# ── Step 1: Videos + Metadata ──────────────────────────────────────────────────
def scrape_videos(client: ApifyClient) -> list[dict]:
    """Search YouTube by each keyword, collect video metadata within the time window."""
    all_videos: list[dict] = []
    seen_ids: set[str] = set()

    for keyword in KEYWORDS:
        print(f"  → keyword: '{keyword}'")
        run = client.actor(ACTOR_YOUTUBE).call(
            run_input={
                "searchQueries": [keyword],
                "maxResults": MAX_VIDEOS_PER_KEYWORD,
                "dateFilter": "month",          # Apify pre-filter: last 30 days
                "sort": "upload_date",          # newest first
                "maxComments": 0,               # comments handled separately
                "proxyConfiguration": {"useApifyProxy": True},
            }
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

        batch_count = 0
        for item in items:
            video_id = item.get("id")
            if not video_id or video_id in seen_ids:
                continue

            pub_date_str = item.get("date") or item.get("uploadDate") or ""
            if not within_window(pub_date_str):
                continue

            seen_ids.add(video_id)
            batch_count += 1
            all_videos.append({
                "video_id":      video_id,
                "title":         item.get("title", ""),
                "url":           item.get("url", f"https://www.youtube.com/watch?v={video_id}"),
                "channel_name":  item.get("channelName", ""),
                "channel_id":    item.get("channelId", ""),
                "published_at":  pub_date_str,
                "duration":      item.get("duration", ""),
                "view_count":    item.get("viewCount", ""),
                "like_count":    item.get("likes", ""),
                "comment_count": item.get("commentsCount", ""),
                "description":   (item.get("description") or "")[:500],
                "keyword":       keyword,
            })

        print(f"     {batch_count} videos within {DAYS_BACK}-day window (total unique: {len(all_videos)})")
        time.sleep(1)

    return all_videos


# ── Step 2: Comments ───────────────────────────────────────────────────────────
def scrape_comments(client: ApifyClient, video_urls: list[str]) -> list[dict]:
    """Fetch top comments for all collected videos in a single actor run."""
    if not video_urls:
        return []

    print(f"  → fetching up to {MAX_COMMENTS_PER_VIDEO} comments for {len(video_urls)} videos")
    run = client.actor(ACTOR_YOUTUBE).call(
        run_input={
            "startUrls": [{"url": url} for url in video_urls],
            "maxResults": 1,
            "maxComments": MAX_COMMENTS_PER_VIDEO,
            "proxyConfiguration": {"useApifyProxy": True},
        }
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    rows: list[dict] = []
    for item in items:
        video_id  = item.get("id", "")
        video_url = item.get("url", "")
        for comment in item.get("comments", []):
            rows.append({
                "video_id":     video_id,
                "video_url":    video_url,
                "comment_id":   comment.get("id", ""),
                "author":       comment.get("authorText", ""),
                "text":         comment.get("text", ""),
                "likes":        comment.get("likesCount", 0),
                "published_at": comment.get("publishedTime", ""),
                "is_reply":     comment.get("isReply", False),
                "reply_to":     comment.get("replyTo", ""),
            })

    print(f"     {len(rows)} comments collected")
    return rows


# ── Step 3: Transcripts ────────────────────────────────────────────────────────
def scrape_transcripts(client: ApifyClient, video_urls: list[str]) -> list[dict]:
    """Fetch transcripts/captions for all collected videos."""
    if not video_urls:
        return []

    print(f"  → fetching transcripts for {len(video_urls)} videos")
    run = client.actor(ACTOR_TRANSCRIPT).call(
        run_input={
            "videoUrls": video_urls,
            "language": "pt",       # Portuguese; falls back to auto-generated if unavailable
        }
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    rows: list[dict] = []
    for item in items:
        transcript_data = item.get("transcript", [])
        if isinstance(transcript_data, list):
            full_text = " ".join(seg.get("text", "") for seg in transcript_data)
            segment_count = len(transcript_data)
        else:
            full_text = str(transcript_data)
            segment_count = 0

        rows.append({
            "video_url":     item.get("videoUrl", ""),
            "language":      item.get("language", ""),
            "full_text":     full_text,
            "segment_count": segment_count,
        })

    print(f"     {len(rows)} transcripts collected")
    return rows


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    if not APIFY_TOKEN:
        raise EnvironmentError("APIFY_TOKEN is not set. Add it to your .env file.")

    client = ApifyClient(APIFY_TOKEN)
    OUTPUT_DIR.mkdir(exist_ok=True)

    sep = "=" * 60
    print(sep)
    print("YouTube Football Scraper — Apify")
    print(f"Period  : last {DAYS_BACK} days (since {CUTOFF_DATE.strftime('%Y-%m-%d')})")
    print(f"Keywords: {len(KEYWORDS)}")
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
    transcripts = scrape_transcripts(client, video_urls)
    save_csv(transcripts, OUTPUT_DIR / "transcripts.csv")

    print(f"\n{sep}")
    print("Done! Output files:")
    print(f"  output/videos.csv      — {len(videos)} videos")
    print(f"  output/comments.csv    — {len(comments)} comments")
    print(f"  output/transcripts.csv — {len(transcripts)} transcripts")
    print(sep)


if __name__ == "__main__":
    main()
