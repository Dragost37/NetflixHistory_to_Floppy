#!/usr/bin/env python3
"""Convert Netflix viewing history CSV into a Floppy import CSV.

This script reads:
- Netflix export CSV (Title, Date)
- A Floppy import template CSV (to reuse exact header format)

And writes a new CSV containing:
- one list row
- one media row per Netflix viewing entry
- one list_item row per Netflix viewing entry
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


DATE_FORMATS = (
    "%m/%d/%y",
    "%m/%d/%Y",
    "%Y-%m-%d",
)

# Segment labels that usually indicate episodic TV metadata in Netflix titles.
TV_MARKERS = {
    "saison",
    "season",
    "episode",
    "episode ",
    "épisode",
    "mini-série",
    "mini-serie",
    "limited series",
    "la série",
    "la serie",
}

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DEFAULT_ENV_FILE = ".env"
TMDB_MAX_RETRIES = 4
ENTRY_FILTERS = {
    "all": {"movie", "tv", "season", "episode"},
    "movies": {"movie"},
    "series": {"tv", "season", "episode"},
    "tv": {"tv"},
    "seasons": {"season"},
    "episodes": {"episode"},
}


@dataclass
class NetflixEntry:
    row_index: int
    watched_at: datetime
    raw_title: str
    title: str
    tmdb_lookup_type: str
    floppy_media_type: str
    series_name: str
    season_number: str = ""
    episode_number: str = ""
    series_position: str = ""
    is_tv_episode: bool = False


@dataclass
class TmdbMatch:
    tmdb_id: str
    related_tv_media_id: str
    media_type: str
    source_url: str
    related_tv_source_url: str
    image: str
    title: str
    original_title: str
    release_date: str
    series_name: str
    season_number: str
    episode_number: str
    series_position: str
    provider_episode_id: str
    runtime_minutes: str
    runtime: str
    vote_average: str
    vote_count: str


def format_duration(seconds: float) -> str:
    if seconds < 0:
        return "--:--"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_args() -> argparse.Namespace:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument(
        "--config",
        default=DEFAULT_ENV_FILE,
        help="Environment config file path (default: .env)",
    )
    bootstrap_args, _ = bootstrap.parse_known_args()
    load_env_file(Path(bootstrap_args.config))

    parser = argparse.ArgumentParser(description="Convert Netflix history CSV to Floppy import CSV")
    parser.add_argument(
        "--config",
        default=bootstrap_args.config,
        help="Environment config file path (default: .env)",
    )
    parser.add_argument(
        "--input",
        default="NetflixViewingHistory.csv",
        help="Netflix history CSV path (default: NetflixViewingHistory.csv)",
    )
    parser.add_argument(
        "--template",
        default="floppy_import_template.csv",
        help="Floppy template CSV path used to read the expected headers",
    )
    parser.add_argument(
        "--output",
        default="floppy_import_from_netflix.csv",
        help="Output Floppy import CSV path",
    )
    parser.add_argument(
        "--list-name",
        default=os.getenv("FLP_LIST_NAME", "Netflix History"),
        help="List name created in Floppy",
    )
    parser.add_argument(
        "--list-uid",
        default=os.getenv("FLP_LIST_UID", "netflix-history"),
        help="List UID used in Floppy",
    )
    parser.add_argument(
        "--tmdb-api-key",
        default=os.getenv("TMDB_API_KEY", ""),
        help="TMDB API key. Defaults to TMDB_API_KEY environment variable.",
    )
    parser.add_argument(
        "--tmdb-language",
        default=os.getenv("TMDB_LANGUAGE", "fr-FR"),
        help="TMDB language for localized titles (default: fr-FR)",
    )
    parser.add_argument(
        "--disable-tmdb",
        action="store_true",
        help="Disable TMDB enrichment and keep manual-only metadata",
    )
    parser.add_argument(
        "--tmdb-cache",
        default="tmdb_cache.json",
        help="Path to local TMDB cache file (default: tmdb_cache.json)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress every N items during conversion (default: 25)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress logs",
    )
    parser.add_argument(
        "--movies-only",
        action="store_true",
        help="Export only movie entries from the Netflix history",
    )
    parser.add_argument(
        "--series-only",
        action="store_true",
        help="Export only series-related entries (tv, season, episode)",
    )
    parser.add_argument(
        "--tv-only",
        action="store_true",
        help="Export only top-level TV entries",
    )
    parser.add_argument(
        "--seasons-only",
        action="store_true",
        help="Export only season entries",
    )
    parser.add_argument(
        "--episodes-only",
        action="store_true",
        help="Export only episode entries",
    )
    parser.add_argument(
        "--entry-filter",
        choices=sorted(ENTRY_FILTERS),
        default="all",
        help="Filter Netflix entries before conversion (default: all)",
    )
    return parser.parse_args()


def parse_date(raw: str) -> datetime:
    value = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {raw!r}")


def clean_whitespace(value: str) -> str:
    # Normalize non-breaking spaces and repeated spaces.
    normalized = value.replace("\u00A0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def is_tv_segment(segment: str) -> bool:
    s = clean_whitespace(segment).casefold()
    return any(s.startswith(marker) for marker in TV_MARKERS)


SEASON_PATTERN = re.compile(r"(?:saison|season)\s*(\d+)\b", re.IGNORECASE)
EPISODE_PATTERN = re.compile(r"(?:épisode|episode|chapitre|chapter|ep(?:isode)?\.?|partie|part)\s*(\d+)\b", re.IGNORECASE)


def format_series_position(season_number: str, episode_number: str) -> str:
    if season_number and episode_number:
        return f"S{int(season_number):02d}E{int(episode_number):02d}"
    if season_number:
        return f"S{int(season_number):02d}"
    if episode_number:
        return f"E{int(episode_number):02d}"
    return ""


def parse_netflix_title(raw_title: str) -> NetflixEntry:
    parts = [clean_whitespace(p) for p in raw_title.split(":")]
    if not parts:
        parts = [clean_whitespace(raw_title)]

    season_number = ""
    episode_number = ""
    tv_hints = 0
    marker_index: int | None = None

    for index, part in enumerate(parts[1:], start=1):
        if is_tv_segment(part):
            tv_hints += 1
            if marker_index is None:
                marker_index = index
        season_match = SEASON_PATTERN.search(part)
        if season_match and not season_number:
            season_number = season_match.group(1)
            tv_hints += 1
            if marker_index is None:
                marker_index = index
        episode_match = EPISODE_PATTERN.search(part)
        if episode_match and not episode_number:
            episode_number = episode_match.group(1)
            tv_hints += 1
            if marker_index is None:
                marker_index = index

    if marker_index is None:
        series_name = parts[0]
    else:
        series_name = clean_whitespace(": ".join(parts[:marker_index])) or parts[0]

    # Netflix often omits the season label for single-season or mini-series entries.
    # Floppy requires both season and episode numbers for episode rows, so default to season 1.
    if episode_number and not season_number:
        season_number = "1"

    tmdb_lookup_type = "tv" if tv_hints > 0 else "movie"
    if episode_number:
        floppy_media_type = "episode"
    elif season_number:
        floppy_media_type = "season"
    elif tmdb_lookup_type == "tv":
        floppy_media_type = "tv"
    else:
        floppy_media_type = "movie"

    title = clean_whitespace(raw_title) if tmdb_lookup_type == "tv" else simplify_title(raw_title)
    return NetflixEntry(
        row_index=0,
        watched_at=datetime.min,
        raw_title=clean_whitespace(raw_title),
        title=title,
        tmdb_lookup_type=tmdb_lookup_type,
        floppy_media_type=floppy_media_type,
        series_name=series_name,
        season_number=season_number,
        episode_number=episode_number,
        series_position=format_series_position(season_number, episode_number),
        is_tv_episode=tmdb_lookup_type == "tv",
    )


def infer_media_type(raw_title: str) -> str:
    parts = [clean_whitespace(p) for p in raw_title.split(":")]
    if any(is_tv_segment(part) for part in parts[1:]):
        return "tv"
    return "movie"


def simplify_title(raw_title: str) -> str:
    parts = [clean_whitespace(p) for p in raw_title.split(":")]
    if len(parts) == 1:
        return parts[0]

    cutoff = None
    for i in range(1, len(parts)):
        if is_tv_segment(parts[i]):
            cutoff = i
            break

    if cutoff is None:
        return ": ".join(parts)

    base = ": ".join(parts[:cutoff]).strip(" :")
    return base or clean_whitespace(raw_title)


def build_base_row(headers: list[str]) -> dict[str, str]:
    return {header: "" for header in headers}


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        # Optional quoted value support.
        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            value = value[1:-1]

        os.environ[key] = value


class TmdbClient:
    def __init__(self, api_key: str, language: str, cache_path: Path) -> None:
        self.api_key = api_key.strip()
        self.language = language
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, str]] = self._load_cache()

    def _load_cache(self) -> dict[str, dict[str, str]]:
        if not self.cache_path.exists():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        return {}

    def persist_cache(self) -> None:
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _request_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        query = {
            "api_key": self.api_key,
            "language": self.language,
            "include_adult": "false",
            **params,
        }
        url = f"https://api.themoviedb.org/3/{endpoint}?{urlencode(query)}"

        for attempt in range(1, TMDB_MAX_RETRIES + 1):
            try:
                with urlopen(url, timeout=15) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 404:
                    raise
                if exc.code in {401, 403}:
                    raise
                if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt == TMDB_MAX_RETRIES:
                    print(
                        f"Warning: TMDB request failed for {endpoint} with HTTP {exc.code}. Falling back to manual metadata.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return {}
            except (URLError, TimeoutError, ConnectionResetError, OSError) as exc:
                if attempt == TMDB_MAX_RETRIES:
                    print(
                        f"Warning: TMDB request failed for {endpoint}: {exc}. Falling back to manual metadata.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return {}

            time.sleep(min(2 ** (attempt - 1), 8))

        return {}

    @staticmethod
    def _candidate_title(item: dict[str, Any]) -> str:
        return clean_whitespace(
            str(item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "")
        )

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        t = clean_whitespace(text).casefold()
        t = re.sub(r"\([^)]*\)", "", t)
        t = re.sub(r"[^\w\s]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def _looks_like_expected_type(cached_data: dict[str, str], expected_type: str) -> bool:
        media_type = str(cached_data.get("media_type") or "")
        source_url = str(cached_data.get("source_url") or "")
        if media_type != expected_type:
            return False
        if expected_type == "movie" and "/movie/" not in source_url:
            return False
        if expected_type == "tv" and "/tv/" not in source_url:
            return False
        return True

    def _pick_best(self, title: str, expected_type: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        filtered = [r for r in results if r.get("media_type") in {"movie", "tv"}]
        if not filtered:
            return None

        exact_type = [r for r in filtered if r.get("media_type") == expected_type]
        if not exact_type:
            return None
        candidates = exact_type

        normalized_query = self._normalize_for_match(title)
        best_item: dict[str, Any] | None = None
        best_score = -1.0

        for item in candidates:
            candidate = self._candidate_title(item)
            normalized_candidate = self._normalize_for_match(candidate)
            ratio = difflib.SequenceMatcher(None, normalized_query, normalized_candidate).ratio()
            type_bonus = 0.15 if item.get("media_type") == expected_type else 0.0
            popularity_bonus = min(float(item.get("popularity") or 0.0) / 500.0, 0.1)
            score = ratio + type_bonus + popularity_bonus

            if score > best_score:
                best_score = score
                best_item = item

        if best_item is None:
            return None
        if best_score < 0.45:
            return None
        return best_item

    @staticmethod
    def _format_runtime(minutes: int | None) -> str:
        if minutes is None or minutes <= 0:
            return ""
        hours, remaining_minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {remaining_minutes:02d}m"
        return f"{remaining_minutes}m"

    @staticmethod
    def _coerce_runtime_minutes(media_type: str, details: dict[str, Any]) -> int | None:
        if media_type == "movie":
            value = details.get("runtime")
            return int(value) if isinstance(value, int) and value > 0 else None

        runtimes = details.get("episode_run_time")
        if isinstance(runtimes, list):
            for value in runtimes:
                if isinstance(value, int) and value > 0:
                    return value
        return None

    def _fetch_details(self, media_type: str, tmdb_id: str) -> dict[str, Any]:
        cache_key = f"details|{media_type}|{tmdb_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            if not cached:
                return {}
            return cached

        details = self._request_json(media_type + f"/{tmdb_id}", {})
        self.cache[cache_key] = details if isinstance(details, dict) else {}
        return details if isinstance(details, dict) else {}

    def _fetch_episode_details(self, tv_id: str, season_number: str, episode_number: str) -> dict[str, Any]:
        cache_key = f"episode|{tv_id}|{season_number}|{episode_number}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            if not cached:
                return {}
            return cached

        try:
            details = self._request_json(f"tv/{tv_id}/season/{season_number}/episode/{episode_number}", {})
        except HTTPError as exc:
            if exc.code == 404:
                self.cache[cache_key] = {}
                return {}
            raise

        self.cache[cache_key] = details if isinstance(details, dict) else {}
        return details if isinstance(details, dict) else {}

    def search(self, title: str, expected_type: str, season_number: str = "", episode_number: str = "") -> TmdbMatch | None:
        cache_key = f"{expected_type}|{title.casefold()}|{season_number}|{episode_number}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            if not cached:
                return None
            cached_data = dict(cached)
            if not self._looks_like_expected_type(cached_data, expected_type):
                self.cache.pop(cache_key, None)
            else:
                cached_data.setdefault("series_name", title)
                cached_data.setdefault("season_number", season_number)
                cached_data.setdefault("episode_number", episode_number)
                cached_data.setdefault("series_position", format_series_position(season_number, episode_number))
                cached_data.setdefault("provider_episode_id", "")
                cached_data.setdefault("related_tv_media_id", cached_data.get("tmdb_id", ""))
                cached_data.setdefault("related_tv_source_url", cached_data.get("source_url", ""))
                cached_data.setdefault("runtime_minutes", "")
                cached_data.setdefault("runtime", "")
                cached_data.setdefault("vote_average", "")
                cached_data.setdefault("vote_count", "")
                return TmdbMatch(**cached_data)

        payload = self._request_json("search/multi", {"query": title})
        results = payload.get("results") if isinstance(payload, dict) else []
        if not isinstance(results, list):
            results = []
        best = self._pick_best(title, expected_type, results)

        match: TmdbMatch | None
        if best is None:
            match = None
        else:
            media_type = str(best.get("media_type"))
            series_tmdb_id = str(best.get("id"))
            tmdb_id = series_tmdb_id
            details = self._fetch_details(media_type, tmdb_id)
            if media_type == "tv" and season_number and episode_number:
                episode_details = self._fetch_episode_details(series_tmdb_id, season_number, episode_number)
                if episode_details:
                    details = episode_details
                    tmdb_id = str(episode_details.get("id") or tmdb_id)
            poster_path = str(details.get("poster_path") or best.get("poster_path") or "")
            image = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path and poster_path != "None" else ""
            localized_title = clean_whitespace(
                str(details.get("title") or details.get("name") or best.get("title") or best.get("name") or self._candidate_title(best))
            )
            original_title = clean_whitespace(
                str(
                    details.get("original_title")
                    or details.get("original_name")
                    or best.get("original_title")
                    or best.get("original_name")
                    or localized_title
                )
            )
            release_date = str(details.get("release_date") or details.get("first_air_date") or best.get("release_date") or best.get("first_air_date") or "")
            vote_average = details.get("vote_average", best.get("vote_average"))
            vote_count = details.get("vote_count", best.get("vote_count"))
            runtime_minutes = self._coerce_runtime_minutes(media_type, details)
            runtime = self._format_runtime(runtime_minutes)
            series_position = format_series_position(season_number, episode_number)
            provider_episode_id = str(details.get("id") or "") if media_type == "tv" and (season_number or episode_number) else ""
            source_url = (
                f"https://www.themoviedb.org/tv/{series_tmdb_id}/season/{season_number}/episode/{episode_number}"
                if media_type == "tv" and season_number and episode_number
                else f"https://www.themoviedb.org/{media_type}/{tmdb_id}"
            )

            match = TmdbMatch(
                tmdb_id=tmdb_id,
                related_tv_media_id=series_tmdb_id if media_type == "tv" else tmdb_id,
                media_type=media_type,
                source_url=source_url,
                related_tv_source_url=f"https://www.themoviedb.org/tv/{series_tmdb_id}" if media_type == "tv" else source_url,
                image=image,
                title=localized_title,
                original_title=original_title,
                release_date=release_date,
                series_name=title,
                season_number=season_number,
                episode_number=episode_number,
                series_position=series_position,
                provider_episode_id=provider_episode_id,
                runtime_minutes=str(runtime_minutes) if runtime_minutes is not None else "",
                runtime=runtime,
                vote_average=(f"{float(vote_average):.1f}" if vote_average is not None else ""),
                vote_count=(str(int(vote_count)) if vote_count is not None else ""),
            )

        self.cache[cache_key] = {} if match is None else {
            "tmdb_id": match.tmdb_id,
            "related_tv_media_id": match.related_tv_media_id,
            "media_type": match.media_type,
            "source_url": match.source_url,
            "related_tv_source_url": match.related_tv_source_url,
            "image": match.image,
            "title": match.title,
            "original_title": match.original_title,
            "release_date": match.release_date,
            "series_name": match.series_name,
            "season_number": match.season_number,
            "episode_number": match.episode_number,
            "series_position": match.series_position,
            "provider_episode_id": match.provider_episode_id,
            "runtime_minutes": match.runtime_minutes,
            "runtime": match.runtime,
            "vote_average": match.vote_average,
            "vote_count": match.vote_count,
        }
        # Stay under TMDB API burst limits for large histories.
        time.sleep(0.05)
        return match


def read_template_headers(template_path: Path) -> list[str]:
    with template_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration as exc:
            raise ValueError("Template CSV is empty") from exc


def load_entries(input_path: Path) -> list[NetflixEntry]:
    entries: list[NetflixEntry] = []

    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"Title", "Date"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Netflix CSV missing required columns: {sorted(missing)}")

        for row_index, row in enumerate(reader, start=1):
            raw_title = clean_whitespace(row["Title"])
            if not raw_title:
                continue

            watch_date = parse_date(row["Date"])
            parsed = parse_netflix_title(raw_title)
            parsed.row_index = row_index
            parsed.watched_at = watch_date
            entries.append(parsed)

    return entries


def extract_movie_entries(entries: list[NetflixEntry]) -> list[NetflixEntry]:
    return [entry for entry in entries if entry.floppy_media_type == "movie"]


def extract_entries_by_type(entries: list[NetflixEntry], allowed_types: set[str]) -> list[NetflixEntry]:
    return [entry for entry in entries if entry.floppy_media_type in allowed_types]


def match_vote_count(match: TmdbMatch | None) -> int:
    if match is None or not match.vote_count:
        return 0
    try:
        return int(match.vote_count)
    except ValueError:
        return 0


def resolve_entry_filter(args: argparse.Namespace) -> str:
    selected_filters = [
        name
        for enabled, name in (
            (args.movies_only, "movies"),
            (args.series_only, "series"),
            (args.tv_only, "tv"),
            (args.seasons_only, "seasons"),
            (args.episodes_only, "episodes"),
        )
        if enabled
    ]

    if args.entry_filter != "all":
        selected_filters.append(args.entry_filter)

    unique_filters = list(dict.fromkeys(selected_filters))
    if len(unique_filters) > 1:
        raise ValueError("Use only one of --entry-filter, --movies-only, --series-only, --tv-only, --seasons-only, or --episodes-only.")

    return unique_filters[0] if unique_filters else "all"


def list_row(headers: list[str], list_uid: str, list_name: str) -> dict[str, str]:
    row = build_base_row(headers)
    row["row_type"] = "list"
    row["list_uid"] = list_uid
    row["list_name"] = list_name
    row["list_description"] = "Imported from Netflix viewing history"
    row["list_visibility"] = "private"
    row["list_include_notes"] = "true"
    row["list_source"] = "netflix"
    row["created_at"] = datetime.now().strftime("%Y-%m-%d")
    return row


def build_parent_tv_row(
    headers: list[str],
    source: str,
    media_id: str,
    title: str,
    original_title: str,
    localized_title: str,
    source_url: str,
    image: str,
    release_date: str,
    provider_rating: str,
    provider_rating_count: str,
    metadata_status: str,
    manual_metadata: str,
    created_at: str,
) -> dict[str, str]:
    row = build_base_row(headers)
    row["row_type"] = "media"
    row["media_id"] = media_id
    row["source"] = source
    row["media_type"] = "tv"
    row["title"] = title
    row["original_title"] = original_title
    row["localized_title"] = localized_title
    row["source_url"] = source_url
    row["image"] = image
    row["release_datetime"] = release_date
    row["provider_rating"] = provider_rating
    row["provider_rating_count"] = provider_rating_count
    row["status"] = "Completed"
    row["notes"] = "Parent TV row required for season/episode imports."
    row["manual_metadata"] = manual_metadata
    row["provider_metadata_status"] = metadata_status
    row["created_at"] = created_at
    row["progressed_at"] = created_at
    row["series_name"] = title
    row["provider_external_ids"] = f"tmdb:{media_id}" if source == "tmdb" else ""
    return row


def media_rows(
    headers: list[str],
    entries: list[NetflixEntry],
    tmdb_client: TmdbClient | None,
    show_progress: bool,
    progress_every: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    parent_tv_rows: dict[tuple[str, str], dict[str, str]] = {}
    ordered = sorted(entries, key=lambda x: x.watched_at, reverse=True)
    total = len(ordered)
    matched_tmdb = 0
    manual_count = 0
    started_at = time.monotonic()

    for idx, entry in enumerate(ordered, start=1):
        lookup_title = entry.series_name if entry.tmdb_lookup_type == "tv" else entry.title
        tmdb_match = tmdb_client.search(
            lookup_title,
            entry.tmdb_lookup_type,
            entry.season_number,
            entry.episode_number,
        ) if tmdb_client else None

        if tmdb_client and entry.tmdb_lookup_type == "movie" and entry.series_name:
            tv_match = tmdb_client.search(
                entry.series_name,
                "tv",
                entry.season_number,
                entry.episode_number,
            )
            if tv_match is not None and (
                tmdb_match is None
                or (tmdb_match.media_type != "tv" and match_vote_count(tmdb_match) == 0 and match_vote_count(tv_match) > 0)
            ):
                tmdb_match = tv_match

        if tmdb_match:
            matched_tmdb += 1
            related_tv_media_id = tmdb_match.related_tv_media_id
            if entry.floppy_media_type in {"season", "episode", "tv"}:
                media_id = related_tv_media_id
            elif entry.floppy_media_type == "movie":
                media_id = tmdb_match.tmdb_id
            else:
                media_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"tmdb::{entry.row_index}::{entry.raw_title.casefold()}::{entry.watched_at.isoformat()}::{tmdb_match.tmdb_id}"))

            media_type = tmdb_match.media_type if entry.floppy_media_type in {"movie", "tv"} else entry.floppy_media_type
            source = "tmdb"
            source_url = tmdb_match.source_url
            image = tmdb_match.image
            display_title = entry.title
            original_title = tmdb_match.original_title
            release_date = tmdb_match.release_date
            series_name = tmdb_match.series_name or entry.series_name
            season_number = tmdb_match.season_number or entry.season_number
            episode_number = tmdb_match.episode_number or entry.episode_number
            series_position = tmdb_match.series_position or entry.series_position
            provider_episode_id = tmdb_match.provider_episode_id
            runtime_minutes = tmdb_match.runtime_minutes
            runtime = tmdb_match.runtime
            provider_rating = tmdb_match.vote_average
            provider_rating_count = tmdb_match.vote_count
            metadata_status = "fetched"
            manual_metadata = ""

            if entry.floppy_media_type in {"season", "episode"}:
                parent_key = (source, related_tv_media_id)
                if parent_key not in parent_tv_rows:
                    parent_tv_rows[parent_key] = build_parent_tv_row(
                        headers=headers,
                        source=source,
                        media_id=related_tv_media_id,
                        title=series_name,
                        original_title=original_title,
                        localized_title=series_name,
                        source_url=tmdb_match.related_tv_source_url,
                        image=image,
                        release_date=release_date,
                        provider_rating=provider_rating,
                        provider_rating_count=provider_rating_count,
                        metadata_status=metadata_status,
                        manual_metadata=manual_metadata,
                        created_at=entry.watched_at.strftime("%Y-%m-%d"),
                    )
        else:
            related_tv_media_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"manual-tv::{entry.series_name.casefold()}"))
            media_id = related_tv_media_id if entry.floppy_media_type in {"season", "episode"} else str(uuid.uuid5(uuid.NAMESPACE_URL, f"netflix::{entry.row_index}::{entry.raw_title.casefold()}::{entry.watched_at.isoformat()}"))
            media_type = entry.floppy_media_type
            source = "manual"
            source_url = ""
            image = ""
            display_title = entry.title
            original_title = ""
            release_date = ""
            series_name = entry.series_name
            season_number = entry.season_number
            episode_number = entry.episode_number
            series_position = entry.series_position
            provider_episode_id = ""
            runtime_minutes = ""
            runtime = ""
            provider_rating = ""
            provider_rating_count = ""
            metadata_status = "manual"
            manual_metadata = "true"
            manual_count += 1

            if entry.floppy_media_type in {"season", "episode"}:
                parent_key = (source, related_tv_media_id)
                if parent_key not in parent_tv_rows:
                    parent_tv_rows[parent_key] = build_parent_tv_row(
                        headers=headers,
                        source=source,
                        media_id=related_tv_media_id,
                        title=series_name,
                        original_title="",
                        localized_title=series_name,
                        source_url="",
                        image="",
                        release_date="",
                        provider_rating="",
                        provider_rating_count="",
                        metadata_status=metadata_status,
                        manual_metadata=manual_metadata,
                        created_at=entry.watched_at.strftime("%Y-%m-%d"),
                    )

        notes = (
            f"Imported from Netflix history. "
            f"Watch date: {entry.watched_at.strftime('%Y-%m-%d')}."
        )

        media = build_base_row(headers)
        media["row_type"] = "media"
        media["media_id"] = media_id
        media["source"] = source
        media["media_type"] = media_type
        media["title"] = display_title
        media["original_title"] = original_title
        media["localized_title"] = display_title
        media["source_url"] = source_url
        media["image"] = image
        media["season_number"] = season_number
        media["episode_number"] = episode_number
        media["provider_episode_id"] = provider_episode_id
        media["series_name"] = series_name
        media["series_position"] = series_position
        media["provider_external_ids"] = f"tmdb:{tmdb_match.tmdb_id}" if tmdb_match and source == "tmdb" else ""
        media["runtime_minutes"] = runtime_minutes
        media["runtime"] = runtime
        media["release_datetime"] = release_date
        media["provider_rating"] = provider_rating
        media["provider_rating_count"] = provider_rating_count
        media["status"] = "Completed"
        media["notes"] = notes
        media["start_date"] = entry.watched_at.strftime("%Y-%m-%d")
        media["end_date"] = entry.watched_at.strftime("%Y-%m-%d")
        media["manual_metadata"] = manual_metadata
        media["provider_metadata_status"] = metadata_status
        media["created_at"] = entry.watched_at.strftime("%Y-%m-%d")
        media["progressed_at"] = entry.watched_at.strftime("%Y-%m-%d")
        rows.append(media)

        if show_progress and (idx == 1 or idx % progress_every == 0 or idx == total):
            elapsed = time.monotonic() - started_at
            rate = idx / elapsed if elapsed > 0 else 0.0
            remaining = total - idx
            eta = remaining / rate if rate > 0 else -1.0
            percent = (idx / total * 100.0) if total else 100.0
            print(
                f"[2/3] Enrichment: {idx}/{total} ({percent:.1f}%) | "
                f"TMDB={matched_tmdb} | manual={manual_count} | "
                f"speed={rate:.2f}/s | ETA={format_duration(eta)}",
                flush=True,
            )

    return [*parent_tv_rows.values(), *rows]


def list_item_rows(
    headers: list[str],
    media_rows_data: list[dict[str, str]],
    list_uid: str,
    list_name: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    for media in media_rows_data:
        item = build_base_row(headers)
        item["row_type"] = "list_item"
        item["media_id"] = media["media_id"]
        item["source"] = media["source"]
        item["media_type"] = media["media_type"]
        item["title"] = media["title"]
        item["original_title"] = media["original_title"]
        item["localized_title"] = media["localized_title"]
        item["source_url"] = media["source_url"]
        item["image"] = media["image"]
        item["season_number"] = media["season_number"]
        item["episode_number"] = media["episode_number"]
        item["provider_episode_id"] = media["provider_episode_id"]
        item["series_name"] = media["series_name"]
        item["series_position"] = media["series_position"]
        item["runtime_minutes"] = media["runtime_minutes"]
        item["runtime"] = media["runtime"]
        item["release_datetime"] = media["release_datetime"]
        item["provider_rating"] = media["provider_rating"]
        item["provider_rating_count"] = media["provider_rating_count"]
        item["provider_external_ids"] = media["provider_external_ids"]
        item["status"] = media["status"]
        item["notes"] = media["notes"]
        item["start_date"] = media["start_date"]
        item["end_date"] = media["end_date"]
        item["created_at"] = media["created_at"]
        item["progressed_at"] = media["progressed_at"]
        item["list_uid"] = list_uid
        item["list_name"] = list_name
        item["list_visibility"] = "private"
        item["list_include_notes"] = "true"
        item["list_source"] = "netflix"
        item["list_item_date_added"] = media["end_date"]
        rows.append(item)

    return rows


def write_output(
    output_path: Path,
    headers: list[str],
    rows: list[dict[str, str]],
) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    run_start = time.monotonic()
    args = parse_args()
    input_path = Path(args.input)
    template_path = Path(args.template)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"Netflix input file not found: {input_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")
    if not args.disable_tmdb and not args.tmdb_api_key.strip():
        raise ValueError(
            "TMDB API key missing. Provide --tmdb-api-key or set TMDB_API_KEY. "
            "Use --disable-tmdb to skip enrichment."
        )

    headers = read_template_headers(template_path)
    required_template_cols = {"row_type", "media_id", "source", "media_type", "title", "list_uid", "list_name"}
    missing_cols = required_template_cols - set(headers)
    if missing_cols:
        raise ValueError(f"Template is missing required columns: {sorted(missing_cols)}")

    show_progress = not args.no_progress
    progress_every = max(1, args.progress_every)
    entry_filter = resolve_entry_filter(args)

    if show_progress:
        print("[1/3] Reading and aggregating Netflix history...", flush=True)
    entries = load_entries(input_path)
    if entry_filter == "movies":
        entries = extract_movie_entries(entries)
    else:
        entries = extract_entries_by_type(entries, ENTRY_FILTERS[entry_filter])
    if show_progress:
        scope_by_filter = {
            "all": "viewing entries",
            "movies": "movie viewing entries",
            "series": "series viewing entries",
            "tv": "tv viewing entries",
            "seasons": "season viewing entries",
            "episodes": "episode viewing entries",
        }
        scope = scope_by_filter[entry_filter]
        print(f"[1/3] Done: {len(entries)} {scope} found.", flush=True)

    tmdb_client = None
    if not args.disable_tmdb:
        if show_progress:
            print("[2/3] TMDB enrichment in progress...", flush=True)
        tmdb_client = TmdbClient(
            api_key=args.tmdb_api_key,
            language=args.tmdb_language,
            cache_path=Path(args.tmdb_cache),
        )
    elif show_progress:
        print("[2/3] TMDB disabled, generating manual entries...", flush=True)

    media = media_rows(headers, entries, tmdb_client, show_progress=show_progress, progress_every=progress_every)

    output_rows: list[dict[str, str]] = []
    if show_progress:
        print("[3/3] Writing Floppy CSV output...", flush=True)
    output_rows.append(list_row(headers, args.list_uid, args.list_name))
    output_rows.extend(media)
    output_rows.extend(list_item_rows(headers, media, args.list_uid, args.list_name))

    write_output(output_path, headers, output_rows)
    if tmdb_client is not None:
        tmdb_client.persist_cache()

    tmdb_count = sum(1 for row in media if row.get("source") == "tmdb")
    elapsed = time.monotonic() - run_start
    print(
        f"Done. Generated {output_path} with {len(media)} media items ({tmdb_count} TMDB matched). "
        f"Total time: {format_duration(elapsed)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
