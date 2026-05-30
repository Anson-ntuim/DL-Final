#!/usr/bin/env python3
"""
Collect a random-ish sample of public YouTube videos from Taiwan-related channels.

This script uses a YouTube Data API key from an environment variable. It does not
use OAuth and can only access public metadata.

Recommended first run:
  export YOUTUBE_API_KEY="YOUR_KEY"
  python collect_tw_youtube_videos.py --target-videos 300

Quota notes:
  - videos.list, channels.list, playlistItems.list cost 1 unit per request.
  - search.list is expensive, so the default discovery mode avoids it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SEARCH_QUERIES = [
    "台灣",
    "台湾",
    "Taiwan",
    "台北",
    "高雄",
    "台中",
    "新北",
    "新聞",
    "美食",
    "旅遊",
    "生活",
    "科技",
    "遊戲",
    "音樂",
    "Podcast",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Randomly collect public videos from Taiwan-related YouTube channels."
    )
    parser.add_argument(
        "--api-key-env",
        default="YOUTUBE_API_KEY",
        help="Environment variable containing the YouTube Data API key.",
    )
    parser.add_argument(
        "--output-csv",
        default="data/tw_youtube_videos.csv",
        help="CSV output path.",
    )
    parser.add_argument(
        "--output-jsonl",
        default="data/tw_youtube_videos.jsonl",
        help="JSONL output path.",
    )
    parser.add_argument(
        "--target-videos",
        type=int,
        default=500,
        help="Write at most this many videos after filtering.",
    )
    parser.add_argument(
        "--candidate-video-multiplier",
        type=float,
        default=1.5,
        help=(
            "Collect this many times the target before category filtering. "
            "Raise it if filters remove too many videos."
        ),
    )
    parser.add_argument(
        "--candidate-channels",
        type=int,
        default=80,
        help="Number of candidate channels to discover before sampling uploads.",
    )
    parser.add_argument(
        "--per-channel-max-pages",
        type=int,
        default=2,
        help="Max uploads playlist pages per channel. Each page is one quota unit.",
    )
    parser.add_argument(
        "--per-channel-video-limit",
        type=int,
        default=8,
        help="Max videos to keep from each channel.",
    )
    parser.add_argument(
        "--published-after",
        default="",
        help="Keep videos published on or after YYYY-MM-DD.",
    )
    parser.add_argument(
        "--published-before",
        default="",
        help="Keep videos published before YYYY-MM-DD.",
    )
    parser.add_argument(
        "--discovery",
        choices=["popular", "search", "mixed"],
        default="popular",
        help=(
            "popular is quota-efficient but biased; search is broader but costly; "
            "mixed uses both."
        ),
    )
    parser.add_argument(
        "--search-queries",
        default="",
        help="Comma-separated search terms for --discovery search/mixed.",
    )
    parser.add_argument(
        "--strict-country",
        action="store_true",
        help="Only keep channels whose public brandingSettings.channel.country is TW.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible sampling.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between API calls.",
    )
    parser.add_argument(
        "--download-thumbnails",
        action="store_true",
        help="Download video thumbnail images locally.",
    )
    parser.add_argument(
        "--thumbnail-dir",
        default="data/thumbnails",
        help="Directory for downloaded thumbnails.",
    )
    parser.add_argument(
        "--thumbnail-quality",
        choices=["maxres", "standard", "high", "medium", "default"],
        default="high",
        help="Preferred thumbnail quality to save/use.",
    )
    parser.add_argument(
        "--exclude-category-ids",
        default="10",
        help="Comma-separated YouTube category IDs to exclude. Default excludes Music (10).",
    )
    return parser.parse_args()


def parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc)


def parse_youtube_time(value: str) -> dt.datetime:
    normalized = value.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(normalized)


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def pick_thumbnail_url(
    thumbnails: dict[str, dict[str, Any]],
    preferred_quality: str,
) -> str:
    fallback_order = ["maxres", "standard", "high", "medium", "default"]
    qualities = [preferred_quality] + [
        quality for quality in fallback_order if quality != preferred_quality
    ]
    for quality in qualities:
        url = thumbnails.get(quality, {}).get("url", "")
        if url:
            return url
    return ""


def download_thumbnail(video_id: str, thumbnail_url: str, thumbnail_dir: Path) -> str:
    if not thumbnail_url:
        return ""

    suffix = Path(thumbnail_url.split("?", 1)[0]).suffix or ".jpg"
    output_path = thumbnail_dir / f"{video_id}{suffix}"
    if output_path.exists():
        return str(output_path)

    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        thumbnail_url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        output_path.write_bytes(response.read())
    return str(output_path)


def is_http_error_reason(error: Exception, reason: str) -> bool:
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return reason in str(content)


class QuotaCounter:
    def __init__(self, sleep_seconds: float = 0.0) -> None:
        self.sleep_seconds = sleep_seconds
        self.requests: dict[str, int] = {}
        self.units = 0

    def add(self, method: str, units: int = 1) -> None:
        self.requests[method] = self.requests.get(method, 0) + 1
        self.units += units
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)


def execute(counter: QuotaCounter, method: str, request: Any, units: int = 1) -> dict[str, Any]:
    counter.add(method, units)
    return request.execute()


def discover_from_popular(
    youtube: Any,
    counter: QuotaCounter,
    wanted_channels: int,
) -> list[str]:
    channel_ids: list[str] = []
    seen: set[str] = set()
    page_token: str | None = None

    while len(channel_ids) < wanted_channels:
        response = execute(
            counter,
            "videos.list(chart=mostPopular)",
            youtube.videos().list(
                part="snippet",
                chart="mostPopular",
                regionCode="TW",
                maxResults=50,
                pageToken=page_token,
            ),
        )
        for item in response.get("items", []):
            channel_id = item.get("snippet", {}).get("channelId")
            if channel_id and channel_id not in seen:
                seen.add(channel_id)
                channel_ids.append(channel_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return channel_ids


def discover_from_search(
    youtube: Any,
    counter: QuotaCounter,
    wanted_channels: int,
    queries: list[str],
) -> list[str]:
    channel_ids: list[str] = []
    seen: set[str] = set()
    shuffled_queries = queries[:]
    random.shuffle(shuffled_queries)
    orders = ["relevance", "date", "videoCount", "viewCount"]

    for query in shuffled_queries:
        if len(channel_ids) >= wanted_channels:
            break
        response = execute(
            counter,
            "search.list(type=channel)",
            youtube.search().list(
                part="snippet",
                type="channel",
                regionCode="TW",
                relevanceLanguage="zh-Hant",
                q=query,
                order=random.choice(orders),
                maxResults=50,
            ),
            units=100,
        )
        for item in response.get("items", []):
            channel_id = item.get("snippet", {}).get("channelId")
            if channel_id and channel_id not in seen:
                seen.add(channel_id)
                channel_ids.append(channel_id)

    return channel_ids


def fetch_channels(
    youtube: Any,
    counter: QuotaCounter,
    channel_ids: list[str],
    strict_country: bool,
) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    for batch in chunks(channel_ids, 50):
        response = execute(
            counter,
            "channels.list",
            youtube.channels().list(
                part="snippet,statistics,contentDetails,brandingSettings",
                id=",".join(batch),
                maxResults=50,
            ),
        )
        for item in response.get("items", []):
            branding = item.get("brandingSettings", {}).get("channel", {})
            country = branding.get("country", "")
            if strict_country and country != "TW":
                continue
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )
            if not uploads:
                continue
            channels.append(
                {
                    "channel_id": item.get("id", ""),
                    "channel_title": item.get("snippet", {}).get("title", ""),
                    "country": country,
                    "uploads_playlist_id": uploads,
                    "subscriber_count": item.get("statistics", {}).get(
                        "subscriberCount", ""
                    ),
                    "channel_view_count": item.get("statistics", {}).get(
                        "viewCount", ""
                    ),
                    "channel_video_count": item.get("statistics", {}).get(
                        "videoCount", ""
                    ),
                }
            )
    return channels


def collect_upload_video_ids(
    youtube: Any,
    counter: QuotaCounter,
    channels: list[dict[str, Any]],
    target_videos: int,
    per_channel_max_pages: int,
    per_channel_video_limit: int,
    published_after: dt.datetime | None,
    published_before: dt.datetime | None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    video_ids: list[str] = []
    seen: set[str] = set()
    channel_by_video_id: dict[str, dict[str, Any]] = {}
    shuffled_channels = channels[:]
    random.shuffle(shuffled_channels)

    for channel in shuffled_channels:
        if len(video_ids) >= target_videos:
            break
        page_token: str | None = None
        channel_candidates: list[str] = []
        for _ in range(per_channel_max_pages):
            try:
                response = execute(
                    counter,
                    "playlistItems.list",
                    youtube.playlistItems().list(
                        part="snippet,contentDetails",
                        playlistId=channel["uploads_playlist_id"],
                        maxResults=50,
                        pageToken=page_token,
                    ),
                )
            except Exception as e:
                if is_http_error_reason(e, "playlistNotFound"):
                    print(
                        "Skipping channel with missing uploads playlist: "
                        f"{channel.get('channel_title', '')} "
                        f"({channel.get('channel_id', '')})",
                        file=sys.stderr,
                    )
                    break
                raise
            for item in response.get("items", []):
                content = item.get("contentDetails", {})
                video_id = content.get("videoId")
                published_at = content.get("videoPublishedAt") or item.get(
                    "snippet", {}
                ).get("publishedAt")
                if not video_id or video_id in seen or not published_at:
                    continue
                published_dt = parse_youtube_time(published_at)
                if published_after and published_dt < published_after:
                    continue
                if published_before and published_dt >= published_before:
                    continue
                seen.add(video_id)
                channel_candidates.append(video_id)
                channel_by_video_id[video_id] = channel
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        random.shuffle(channel_candidates)
        for video_id in channel_candidates[:per_channel_video_limit]:
            video_ids.append(video_id)
            if len(video_ids) >= target_videos:
                break

    random.shuffle(video_ids)
    return video_ids[:target_videos], channel_by_video_id


def fetch_video_rows(
    youtube: Any,
    counter: QuotaCounter,
    video_ids: list[str],
    channel_by_video_id: dict[str, dict[str, Any]],
    thumbnail_quality: str,
    download_thumbnails: bool,
    thumbnail_dir: Path,
    excluded_category_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat()

    for batch in chunks(video_ids, 50):
        response = execute(
            counter,
            "videos.list",
            youtube.videos().list(
                part="snippet,statistics,contentDetails,status,topicDetails",
                id=",".join(batch),
                maxResults=50,
            ),
        )
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            content = item.get("contentDetails", {})
            status = item.get("status", {})
            category_id = snippet.get("categoryId", "")
            if category_id in excluded_category_ids:
                continue
            channel = channel_by_video_id.get(item.get("id", ""), {})
            tags = snippet.get("tags", [])
            video_id = item.get("id", "")
            thumbnail_url = pick_thumbnail_url(
                snippet.get("thumbnails", {}),
                preferred_quality=thumbnail_quality,
            )
            thumbnail_path = ""
            if download_thumbnails:
                thumbnail_path = download_thumbnail(video_id, thumbnail_url, thumbnail_dir)
            rows.append(
                {
                    "video_id": video_id,
                    "title": snippet.get("title", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "category_id": category_id,
                    "duration_iso8601": content.get("duration", ""),
                    "definition": content.get("definition", ""),
                    "caption": content.get("caption", ""),
                    "licensed_content": content.get("licensedContent", ""),
                    "privacy_status": status.get("privacyStatus", ""),
                    "view_count": statistics.get("viewCount", ""),
                    "like_count": statistics.get("likeCount", ""),
                    "comment_count": statistics.get("commentCount", ""),
                    "favorite_count": statistics.get("favoriteCount", ""),
                    "tags_count": len(tags),
                    "tags": "|".join(tags),
                    "thumbnail_url": thumbnail_url,
                    "thumbnail_path": thumbnail_path,
                    "description_length": len(snippet.get("description", "")),
                    "default_language": snippet.get("defaultLanguage", ""),
                    "default_audio_language": snippet.get("defaultAudioLanguage", ""),
                    "channel_country": channel.get("country", ""),
                    "subscriber_count": channel.get("subscriber_count", ""),
                    "channel_view_count": channel.get("channel_view_count", ""),
                    "channel_video_count": channel.get("channel_video_count", ""),
                    "retrieved_at": retrieved_at,
                }
            )

    random.shuffle(rows)
    return rows


def write_outputs(rows: list[dict[str, Any]], csv_path: Path, jsonl_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ModuleNotFoundError:
        print(
            "Missing dependency. Run: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"Missing API key env var: {args.api_key_env}", file=sys.stderr)
        return 1

    search_queries = [
        q.strip() for q in args.search_queries.split(",") if q.strip()
    ] or DEFAULT_SEARCH_QUERIES
    excluded_category_ids = {
        category_id.strip()
        for category_id in args.exclude_category_ids.split(",")
        if category_id.strip()
    }
    published_after = parse_date(args.published_after)
    published_before = parse_date(args.published_before)
    counter = QuotaCounter(sleep_seconds=args.sleep)
    youtube = build("youtube", "v3", developerKey=api_key)

    try:
        channel_ids: list[str] = []
        if args.discovery in {"popular", "mixed"}:
            channel_ids.extend(
                discover_from_popular(youtube, counter, args.candidate_channels)
            )
        if args.discovery in {"search", "mixed"} and len(channel_ids) < args.candidate_channels:
            channel_ids.extend(
                discover_from_search(
                    youtube,
                    counter,
                    args.candidate_channels - len(set(channel_ids)),
                    search_queries,
                )
            )

        deduped_channel_ids = list(dict.fromkeys(channel_ids))
        random.shuffle(deduped_channel_ids)
        deduped_channel_ids = deduped_channel_ids[: args.candidate_channels]
        channels = fetch_channels(
            youtube, counter, deduped_channel_ids, strict_country=args.strict_country
        )

        candidate_video_target = max(
            args.target_videos,
            math.ceil(args.target_videos * args.candidate_video_multiplier),
        )
        video_ids, channel_by_video_id = collect_upload_video_ids(
            youtube,
            counter,
            channels,
            candidate_video_target,
            args.per_channel_max_pages,
            args.per_channel_video_limit,
            published_after,
            published_before,
        )
        rows = fetch_video_rows(
            youtube,
            counter,
            video_ids,
            channel_by_video_id,
            args.thumbnail_quality,
            args.download_thumbnails,
            Path(args.thumbnail_dir),
            excluded_category_ids,
        )
        rows = rows[: args.target_videos]
        write_outputs(rows, Path(args.output_csv), Path(args.output_jsonl))
    except HttpError as e:
        print("YouTube Data API request failed:", file=sys.stderr)
        print(e, file=sys.stderr)
        return 2

    print(f"Discovered channels: {len(deduped_channel_ids)}")
    print(f"Channels kept: {len(channels)}")
    print(f"Videos written: {len(rows)}")
    print(f"CSV: {args.output_csv}")
    print(f"JSONL: {args.output_jsonl}")
    print(f"Estimated quota units used: {counter.units}")
    print("Requests:")
    for method, count in sorted(counter.requests.items()):
        print(f"  - {method}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
