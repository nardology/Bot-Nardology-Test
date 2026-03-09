# scripts/migrate_json_to_sqlite.py
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# --- Ensure project root is on sys.path ---
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils.storage import set_guild_setting  # noqa: E402

DATA_DIR = BASE_DIR / "data"
JSON_PRIMARY = DATA_DIR / "settings.json"
JSON_BACKUP = DATA_DIR / "settings.backup.json"


def _pick_source_file() -> Path | None:
    if JSON_PRIMARY.exists():
        return JSON_PRIMARY
    if JSON_BACKUP.exists():
        return JSON_BACKUP
    return None


def main() -> None:
    src = _pick_source_file()
    if src is None:
        print("No settings.json or settings.backup.json found, nothing to migrate.")
        return

    raw = src.read_text(encoding="utf-8").strip()

    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {src.name}: {e}")
        return

    if not isinstance(data, dict):
        print("settings file is not a dict, aborting.")
        return

    migrated = 0
    per_guild: dict[int, int] = {}

    for guild_id_str, settings in data.items():
        try:
            gid = int(guild_id_str)
        except ValueError:
            continue

        if not isinstance(settings, dict):
            continue

        for key, value in settings.items():
            if not isinstance(key, str) or not key.strip():
                continue

            set_guild_setting(gid, key, value)
            migrated += 1
            per_guild[gid] = per_guild.get(gid, 0) + 1

    print(f"✅ Migrated {migrated} settings entries into SQLite.")
    if per_guild:
        for gid, count in per_guild.items():
            print(f"  • Guild {gid}: {count} keys")

    # Rename source file so it’s obvious it was already migrated
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(src.stem + f".migrated.{ts}" + src.suffix)
    try:
        src.rename(dst)
        print(f"🧹 Renamed source file to: {dst.name}")
    except Exception:
        print("⚠️ Migration succeeded, but could not rename the source file.")


if __name__ == "__main__":
    main()
