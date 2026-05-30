#!/usr/bin/env python3
"""
使用 YouTube Analytics API (reports.query) 抓取指定影片的分析資料，
並輸出欄位與型態，方便快速檢查資料結構。

使用前準備：
1) 到 Google Cloud Console 啟用 YouTube Analytics API
2) 建立 OAuth 用戶端 (Desktop app) 並下載 client secret JSON
3) 安裝套件：pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query YouTube Analytics reports.query and inspect data types."
    )
    parser.add_argument(
        "--client-secrets",
        default="client_secret.json",
        help="OAuth client secrets JSON 路徑 (預設: client_secret.json)",
    )
    parser.add_argument(
        "--token-file",
        default="token.json",
        help="儲存 OAuth token 的路徑 (預設: token.json)",
    )
    parser.add_argument(
        "--video-ids",
        default="",
        help="影片 ID 清單，逗號分隔，例如: abc123,def456；不填則抓整個頻道影片",
    )
    parser.add_argument(
        "--start-date",
        default=(dt.date.today() - dt.timedelta(days=28)).isoformat(),
        help="開始日期 YYYY-MM-DD (預設: 今天往前 28 天)",
    )
    parser.add_argument(
        "--end-date",
        default=(dt.date.today() - dt.timedelta(days=1)).isoformat(),
        help="結束日期 YYYY-MM-DD (預設: 昨天)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="API 最多抓幾筆原始資料 (預設: 100)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="從原始資料隨機抽樣幾筆顯示 (預設: 10)",
    )
    return parser.parse_args()


def get_credentials(client_secrets_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path), SCOPES
        )
        creds = flow.run_local_server(port=0)

    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_query_params(args: argparse.Namespace) -> dict[str, Any]:
    video_ids = [v.strip() for v in args.video_ids.split(",") if v.strip()]
    query_params = {
        "ids": "channel==MINE",
        "startDate": args.start_date,
        "endDate": args.end_date,
        "metrics": "views,estimatedMinutesWatched,averageViewDuration,likes,comments,shares",
        "dimensions": "video,day",
        "sort": "-views",
        "maxResults": args.max_results,
    }
    if video_ids:
        query_params["filters"] = f"video=={','.join(video_ids)}"
    return query_params


def print_type_inspection(response: dict[str, Any], sample_size: int) -> None:
    columns = response.get("columnHeaders", [])
    rows = response.get("rows", [])

    print("\n=== Column Headers ===")
    for idx, c in enumerate(columns):
        print(
            f"[{idx}] name={c.get('name')}, columnType={c.get('columnType')}, dataType={c.get('dataType')}"
        )

    print(f"\n=== Row Count === {len(rows)}")
    if not rows:
        print("沒有資料，請調整日期區間或影片 ID。")
        return

    actual_sample_size = min(sample_size, len(rows))
    sampled_rows = random.sample(rows, k=actual_sample_size)
    print(f"\n=== Random Sample Rows (value + python type), k={actual_sample_size} ===")
    if sample_size > len(rows):
        print(f"資料不足 {sample_size} 筆，改為顯示全部 {len(rows)} 筆。")

    preview_rows = sampled_rows
    for r_i, row in enumerate(preview_rows):
        print(f"\nRow #{r_i}:")
        for c_i, value in enumerate(row):
            col_name = columns[c_i]["name"] if c_i < len(columns) else f"col_{c_i}"
            print(f"  - {col_name}: {value!r} (python_type={type(value).__name__})")

    print("\n=== Raw JSON (truncated preview) ===")
    raw = json.dumps(response, ensure_ascii=False, indent=2)
    print(raw[:2000] + ("\n... (truncated)" if len(raw) > 2000 else ""))


def main() -> int:
    args = parse_args()
    client_secrets = Path(args.client_secrets)
    token_file = Path(args.token_file)

    if not client_secrets.exists():
        print(f"找不到 client secrets 檔案: {client_secrets}", file=sys.stderr)
        return 1

    try:
        creds = get_credentials(client_secrets, token_file)
        service = build("youtubeAnalytics", "v2", credentials=creds)
        query_params = build_query_params(args)
        response = service.reports().query(**query_params).execute()
        print_type_inspection(response, sample_size=args.sample_size)
        return 0
    except HttpError as e:
        print("YouTube Analytics API 呼叫失敗：", file=sys.stderr)
        print(e, file=sys.stderr)
        return 2
    except Exception as e:  # pragma: no cover - quick debug helper
        print("執行失敗：", file=sys.stderr)
        print(e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
