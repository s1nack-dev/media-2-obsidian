#!/usr/bin/env python3
"""
Polls a private YouTube playlist for new videos and processes each one
through pipeline.process_input() (subtitles/transcription + AI summary +
GitHub + Obsidian commits).

Run this on a schedule (cron on macOS, see README) or, if containerized,
with --loop (no external scheduler reaches into a container). Each pass:
  1. Reads your private "to summarize" YouTube playlist.
  2. Finds videos not yet processed (tracked in state.json).
  3. Processes each new video, retrying transient failures up to
     max_retries times across future passes before giving up and
     notifying you.

See README.md for full setup instructions.
"""
import argparse
import logging
import subprocess
import sys
import time
import traceback
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core import load_config, load_state, notify, pipeline_lock, resolve_secret, save_state
from pipeline import NoTranscriptAvailableError, process_input

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_playlist")

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
DEFAULT_MAX_RETRIES = 3


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------

def get_youtube_service(cfg: dict):
    """
    Create an authenticated YouTube Data API service from the configured OAuth token.
    
    Parameters:
        cfg (dict): Configuration containing the YouTube OAuth token file path.
    
    Returns:
        Resource: An authenticated YouTube Data API service.
    
    Raises:
        SystemExit: If the configured OAuth token file does not exist.
    """
    token_file = cfg["youtube"]["token_file"]
    if not Path(token_file).exists():
        log.error(
            "No %s found. Run `python youtube_auth.py --config config.yaml` "
            "once first (see README.md).", token_file
        )
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(token_file).write_text(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def get_playlist_items(service, playlist_id: str) -> list[dict]:
    """
    Retrieve all videos in a YouTube playlist in oldest-first order.
    
    Parameters:
    	playlist_id (str): The ID of the playlist to retrieve.
    
    Returns:
    	list[dict]: Playlist items containing `video_id`, `title`, and `published_at`.
    """
    items = []
    page_token = None
    while True:
        resp = service.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for it in resp.get("items", []):
            items.append({
                "video_id": it["contentDetails"]["videoId"],
                "title": it["snippet"]["title"],
                "published_at": it["snippet"].get("publishedAt", ""),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_once(cfg: dict) -> None:
    """
    Process each unprocessed playlist video and persist its outcome.
    
    Parameters:
        cfg (dict): Application configuration, including playlist, state, secret,
            locking, and retry settings.
    """
    state = load_state(cfg["state_file"])
    processed = set(state["processed_video_ids"])

    try:
        github_token = resolve_secret("GITHUB_TOKEN", cfg["github"]["token_op_ref"])
        bridge_token = resolve_secret("BRIDGE_AUTH_TOKEN", cfg["bridge"]["auth_token_op_ref"])
    except subprocess.CalledProcessError as e:
        log.error("Failed to read a secret from 1Password: %s", e)
        sys.exit(1)

    service = get_youtube_service(cfg)
    items = get_playlist_items(service, cfg["youtube"]["playlist_id"])
    new_items = [it for it in items if it["video_id"] not in processed]

    if not new_items:
        log.info("No new videos in playlist.")
        return

    log.info("Found %d new video(s).", len(new_items))
    max_retries = cfg.get("max_retries", DEFAULT_MAX_RETRIES)

    lock_path = cfg.get("lock_file", "pipeline.lock")

    for item in new_items:
        video_id, title = item["video_id"], item["title"]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        log.info("Processing %s (%s)", video_id, title)

        try:
            with pipeline_lock(lock_path):
                process_input(
                    video_url, cfg,
                    item_hint={"video_id": video_id, "title": title, "published_at": item["published_at"]},
                    github_token=github_token, bridge_token=bridge_token,
                )
                processed.add(video_id)
                state["failed_attempts"].pop(video_id, None)
                state["processed_video_ids"] = sorted(processed)
                save_state(cfg["state_file"], state)
            log.info("Done with %s.", video_id)

        except NoTranscriptAvailableError:
            with pipeline_lock(lock_path):
                log.warning("No subtitles/transcript available for %s, skipping permanently.", video_id)
                notify(
                    cfg, "Pipeline: no subtitles available",
                    f'"{title}" ({video_url}) has no captions and could not be transcribed. '
                    "Skipped, will not be retried.",
                )
                processed.add(video_id)
                state["processed_video_ids"] = sorted(processed)
                save_state(cfg["state_file"], state)

        except Exception:
            err = traceback.format_exc()
            log.error("Failed processing %s (%s):\n%s", video_id, title, err)

            with pipeline_lock(lock_path):
                attempts = state["failed_attempts"].get(video_id, 0) + 1
                state["failed_attempts"][video_id] = attempts
                save_state(cfg["state_file"], state)

                if attempts >= max_retries:
                    notify(
                        cfg, f"Pipeline: giving up on a video after {attempts} failures",
                        f'"{title}" ({video_url}) failed {attempts} times and will not be retried again.'
                        f"\n\nLast error:\n{err[-2000:]}",
                    )
                    processed.add(video_id)
                    state["processed_video_ids"] = sorted(processed)
                    save_state(cfg["state_file"], state)
                else:
                    notify(
                        cfg, f"Pipeline: run failed (attempt {attempts}/{max_retries})",
                        f'"{title}" ({video_url}) failed, will retry on the next run.\n\nError:\n{err[-2000:]}',
                    )
            # continue on to the next video rather than aborting the whole run
            continue


def main():
    """
    Run one playlist-processing pass or continuously poll at a configured interval.
    
    Command-line options select the configuration file, loop mode, and delay between
    polling passes. In loop mode, errors from an individual pass are logged and
    reported before processing resumes after the configured interval.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--loop", action="store_true", help="Run forever, polling every --interval "
                         "seconds instead of exiting after one pass. For a containerized deployment "
                         "with no external scheduler reaching into the container; use plain cron "
                         "instead if running natively.")
    parser.add_argument("--interval", type=int, default=1800,
                         help="Seconds between polls in --loop mode. Default 1800 (30 min).")
    args = parser.parse_args()

    if args.loop and args.interval <= 0:
        parser.error("--interval must be a positive integer when using --loop")

    cfg = load_config(args.config)

    if not args.loop:
        run_once(cfg)
        return

    log.info("Running in --loop mode, polling every %d seconds.", args.interval)
    while True:
        try:
            run_once(cfg)
        except Exception:
            # A single bad pass shouldn't kill a long-running container -
            # log it, notify, and try again next interval instead of
            # exiting (which the non-loop path still does, via the
            # __main__ handler below).
            err = traceback.format_exc()
            log.error("Loop pass failed (will retry next interval):\n%s", err)
            try:
                notify(cfg, "Pipeline: loop pass crashed", err[-2000:])
            except Exception:
                pass
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        log.error("Fatal error:\n%s", err)
        try:
            _cfg = load_config(
                (sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else "config.yaml")
            )
            notify(_cfg, "Pipeline: run crashed", err[-2000:])
        except Exception:
            pass
        sys.exit(1)
