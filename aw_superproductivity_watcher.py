#!/usr/bin/env python3

import os
import json
import time
import socket
import argparse
from datetime import datetime, timezone

from aw_core.models import Event
from aw_client import ActivityWatchClient

# Configuration
DEFAULT_BACKUP_FILE = '/tmp/superprod_backup.json'
DEFAULT_CHECK_INTERVAL = 60  # seconds

HOSTNAME = socket.gethostname()
CLIENT_NAME = "aw-watcher-superproductivity"
BUCKET_ID = f"{CLIENT_NAME}_{HOSTNAME}"
EVENT_TYPE = "superprod-activity"

# Keep track of last processed backup
last_mtime = 0
last_session_end_ms = 0
last_update_timestamp = 0

# Persistent cache for previous_time_spent
CACHE_FILE = "previous_time_spent_cache.json"
previous_time_spent_cache = {}

def load_cache():
    global previous_time_spent_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                previous_time_spent_cache = json.load(f)
        except Exception as e:
            print(f"[WARN] Failed to load cache: {e}")
            previous_time_spent_cache = {}
    else:
        previous_time_spent_cache = {}

def save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(previous_time_spent_cache, f)
    except Exception as e:
        print(f"[WARN] Failed to save cache: {e}")


def get_backup_file_and_interval():
    parser = argparse.ArgumentParser(description="Super Productivity to ActivityWatch Exporter")
    parser.add_argument('--file', type=str, default=DEFAULT_BACKUP_FILE, help='Path to Super Productivity backup file or directory')
    parser.add_argument('--interval', type=int, default=DEFAULT_CHECK_INTERVAL, help='Check interval in seconds')

    args = parser.parse_args()

    print(f"[INFO] Using backup file or directory: {args.file}")
    print(f"[INFO] Using check interval: {args.interval} seconds")

    return args.file, args.interval


BACKUP_FILE, CHECK_INTERVAL = get_backup_file_and_interval()


client = ActivityWatchClient(CLIENT_NAME)
client.create_bucket(BUCKET_ID, event_type=EVENT_TYPE)


def resolve_backup_file_path():
    if os.path.isdir(BACKUP_FILE):
        json_files = [os.path.join(BACKUP_FILE, f) for f in os.listdir(BACKUP_FILE) if f.endswith('.json') or f == '__meta_']
        if not json_files:
            raise FileNotFoundError(f"No suitable files found in directory: {BACKUP_FILE}")
        latest_file = max(json_files, key=os.path.getmtime)
        print(f"[INFO] Resolved latest backup file: {latest_file}")
        return latest_file
    else:
        return BACKUP_FILE


def parse_backup_and_send():
    global last_session_end_ms, last_update_timestamp, previous_time_spent_cache
    backup_file = resolve_backup_file_path()

    try:
        with open(backup_file) as f:
            for line in f:
                if line.startswith('pf_'):
                    json_part_index = line.find('{')
                    if json_part_index != -1:
                        json_data = json.loads(line[json_part_index:])
                        break
            else:
                print(f"[ERROR] No valid JSON data found in file: {backup_file}")
                return
    except Exception as e:
        print(f"[ERROR] Failed to read backup file: {e}")
        return

    last_update = json_data.get('lastUpdate', 0)
    if last_update <= last_update_timestamp:
        print("[INFO] No new updates since last check.")
        return
    last_update_timestamp = last_update

    mainData = json_data.get('mainModelData', {})
    tasks = mainData.get('task', {}).get('entities', {})
    projects = json_data.get('project', {}).get('entities', {})
    project_names = {p_id: p_data.get('title', 'Unnamed Project') for p_id, p_data in projects.items()}

    time_tracking_data = mainData.get('timeTracking', {})

    time_tracking_sources = {
        "project": time_tracking_data.get('project', {}),
        "tag": time_tracking_data.get('tag', {})
    }

    for source_name, time_tracking in time_tracking_sources.items():
        for project_id, days in time_tracking.items():
            project_name = project_names.get(project_id, project_id)
            for day, session in days.items():
                start_ms = session.get('s')
                end_ms = session.get('e')
                if start_ms and end_ms and end_ms > last_session_end_ms:
                    timestamp_start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
                    duration_sec = (end_ms - start_ms) / 1000

                    associated_tasks = [
                        t for t in tasks.values()
                        if t.get('projectId') == project_id and day in t.get('timeSpentOnDay', {})
                    ]
                    for task in associated_tasks:
                        task_id = str(task.get('id'))
                        time_spent_today = task.get('timeSpentOnDay', {}).get(day, 0)
                        prev_time_spent = previous_time_spent_cache.get(task_id, {}).get(day, 0)
                        time_diff = time_spent_today - prev_time_spent
                        # Update cache for next run
                        if task_id not in previous_time_spent_cache:
                            previous_time_spent_cache[task_id] = {}
                        previous_time_spent_cache[task_id][day] = time_spent_today

                        title = task.get('title', 'Unnamed Task')
                        event_data = {
                            "source": source_name,
                            "project_or_tag_id": project_name,
                            "title": title
                        }
                        event = Event(timestamp=timestamp_start, duration=duration_sec, data=event_data)
                        client.insert_event(BUCKET_ID, event)

                        print(f"[INFO] Sent session for {source_name} '{project_name}' with task [{title}] on {day} time diff={time_diff} starting at {timestamp_start.isoformat()} ({duration_sec}s)")

                    if end_ms > last_session_end_ms:
                        last_session_end_ms = end_ms

    save_cache()


def watcher_loop():
    global last_mtime
    print("[INFO] Starting Super Productivity Watcher...")

    while True:
        try:
            current_backup_file = resolve_backup_file_path()
            current_mtime = os.path.getmtime(current_backup_file)

            if last_mtime == 0 or current_mtime != last_mtime:
                print(f"[INFO] Detected new backup at {datetime.now(timezone.utc).isoformat()}.")
                parse_backup_and_send()
                last_mtime = current_mtime
            else:
                print("[INFO] No new backup detected.")

        except FileNotFoundError as e:
            print(f"[WARN] {e}")
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    load_cache()
    watcher_loop()
