#!/usr/bin/env python3
import csv
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STRAVA_ACTIVITY_DETAIL_URL = "https://www.strava.com/api/v3/activities/{activity_id}"


class StravaSyncError(RuntimeError):
    pass


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise StravaSyncError(
            f"config file not found: {config_path}. copy config.example.json to config.json"
        )
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StravaSyncError(f"invalid config json: {e}") from e


def get_config_value(cfg: dict, path: str) -> str:
    cur = cfg
    for p in path.split('.'):
        if not isinstance(cur, dict) or p not in cur:
            raise StravaSyncError(f"missing config key: {path}")
        cur = cur[p]
    if not isinstance(cur, str) or not cur.strip():
        raise StravaSyncError(f"config key must be non-empty string: {path}")
    return cur


def ensure_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise StravaSyncError(f"token refresh failed: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def fetch_activities(access_token: str, after_epoch: int):
    headers = {"Authorization": f"Bearer {access_token}"}
    page = 1
    per_page = 100
    while True:
        params = {"after": after_epoch, "page": page, "per_page": per_page}
        resp = requests.get(STRAVA_ACTIVITIES_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise StravaSyncError(f"activities fetch failed: {resp.status_code} {resp.text}")
        items = resp.json()
        if not items:
            return
        for item in items:
            yield item
        if len(items) < per_page:
            return
        page += 1


def fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = STRAVA_ACTIVITY_DETAIL_URL.format(activity_id=activity_id)
    resp = requests.get(url, headers=headers, params={"include_all_efforts": "false"}, timeout=30)
    if resp.status_code != 200:
        raise StravaSyncError(f"activity detail failed: {resp.status_code} {resp.text}")
    return resp.json()


def build_default_km_laps(activity: dict) -> list[dict]:
    total_distance = float(activity.get("distance", 0) or 0)
    moving_time = float(activity.get("moving_time", 0) or 0)
    if total_distance <= 0 or moving_time <= 0:
        return []

    km = 1000.0
    speed = total_distance / moving_time
    laps = []
    covered = 0.0
    idx = 1
    while covered < total_distance:
        d = min(km, total_distance - covered)
        t = d / speed if speed > 0 else 0
        laps.append({
            "lap_index": idx,
            "distance": d,
            "moving_time": round(t, 2),
            "elapsed_time": round(t, 2),
            "average_speed": speed,
            "max_speed": speed,
            "total_elevation_gain": None,
            "average_heartrate": None,
            "max_heartrate": None,
            "source": "default_1km",
        })
        covered += d
        idx += 1
    return laps


def normalize_laps(detail: dict, summary_activity: dict) -> list[dict]:
    laps = detail.get("laps") or []
    is_interval = str(summary_activity.get("workout_type", "")) == "3"
    if laps:
        for i, lap in enumerate(laps, 1):
            lap["lap_index"] = i
            lap["source"] = "strava_lap"
        return laps

    splits = detail.get("splits_standard") or []
    if splits:
        for i, split in enumerate(splits, 1):
            split["lap_index"] = i
            split["source"] = "splits_standard"
        return splits

    if not is_interval:
        return build_default_km_laps(summary_activity)
    return []


def append_laps_csv(csv_path: Path, activity: dict, laps: list[dict]) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "activity_id", "lap_id", "lap_index", "sport_type", "start_date", "distance_m",
        "moving_time_sec", "elapsed_time_sec", "avg_speed_mps", "max_speed_mps",
        "avg_hr", "max_hr", "elevation_gain_m", "source", "sync_time_utc",
    ]
    seen = set()
    if csv_path.exists():
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen.add((r.get("activity_id", ""), r.get("lap_id", ""), r.get("lap_index", "")))

    write_header = not csv_path.exists()
    written = 0
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for lap in laps:
            key = (str(activity.get("id", "")), str(lap.get("id", "")), str(lap.get("lap_index", "")))
            if key in seen:
                continue
            row = {
                "activity_id": activity.get("id"),
                "lap_id": lap.get("id", ""),
                "lap_index": lap.get("lap_index", ""),
                "sport_type": activity.get("sport_type", activity.get("type", "Unknown")),
                "start_date": activity.get("start_date", ""),
                "distance_m": lap.get("distance", ""),
                "moving_time_sec": lap.get("moving_time", ""),
                "elapsed_time_sec": lap.get("elapsed_time", ""),
                "avg_speed_mps": lap.get("average_speed", ""),
                "max_speed_mps": lap.get("max_speed", ""),
                "avg_hr": lap.get("average_heartrate", ""),
                "max_hr": lap.get("max_heartrate", ""),
                "elevation_gain_m": lap.get("total_elevation_gain", ""),
                "source": lap.get("source", ""),
                "sync_time_utc": datetime.now(timezone.utc).isoformat(),
            }
            w.writerow(row)
            seen.add(key)
            written += 1
    return written


def main() -> None:
    config_path = Path("config.json")
    cfg = load_config(config_path)

    client_id = get_config_value(cfg, "strava.client_id")
    client_secret = get_config_value(cfg, "strava.client_secret")
    refresh_token = get_config_value(cfg, "strava.refresh_token")

    data_dir = Path(get_config_value(cfg, "paths.data_dir"))
    obsidian_dir = Path(get_config_value(cfg, "paths.obsidian_dir"))
    activities_dir = data_dir / "activities"
    laps_dir = data_dir / "laps"
    laps_csv = data_dir / "laps.csv"

    data_dir.mkdir(parents=True, exist_ok=True)
    activities_dir.mkdir(parents=True, exist_ok=True)
    laps_dir.mkdir(parents=True, exist_ok=True)
    obsidian_dir.mkdir(parents=True, exist_ok=True)

    conn = ensure_db(data_dir / "state.db")
    last_epoch = int(get_state(conn, "last_activity_epoch", "0") or "0")

    token = get_access_token(client_id, client_secret, refresh_token)

    max_epoch = last_epoch
    count = 0
    lap_rows = 0

    for activity in fetch_activities(token, last_epoch):
        aid = activity["id"]
        (activities_dir / f"{aid}.json").write_text(json.dumps(activity, ensure_ascii=False, indent=2), encoding="utf-8")

        detail = fetch_activity_detail(token, aid)
        (laps_dir / f"{aid}_detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        laps = normalize_laps(detail, activity)
        lap_rows += append_laps_csv(laps_csv, activity, laps)

        start_utc = activity.get("start_date", "")
        try:
            epoch = int(datetime.fromisoformat(start_utc.replace("Z", "+00:00")).timestamp())
            max_epoch = max(max_epoch, epoch)
        except ValueError:
            pass
        count += 1

    if max_epoch > last_epoch:
        set_state(conn, "last_activity_epoch", str(max_epoch))

    print(f"sync done, activities: {count}, new lap rows: {lap_rows}, cursor: {last_epoch} -> {max_epoch}")


if __name__ == "__main__":
    main()
