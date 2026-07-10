import os
import json
from pathlib import Path
from Database.database import Database

def main():
    """Iterate through all user database folders and clear duplicate entries."""
    base_dir = Path(__file__).resolve().parent
    users_dir = base_dir / "Database" / "Users"
    
    if not users_dir.exists():
        print(f"Users directory not found at: {users_dir}")
        return

    print("Checking for duplicate database entries...")
    user_count = 0
    total_removed = 0

    for user_folder in users_dir.iterdir():
        if user_folder.is_dir() and (user_folder / "entries.json").exists():
            username = user_folder.name
            print(f"Processing database for user: {username}")
            try:
                db = Database(user=username)
                removed = db.deduplicate()
                if removed > 0:
                    print(f"  -> Removed {removed} duplicate entries.")
                    total_removed += removed
                else:
                    print("  -> No duplicates found.")
                user_count += 1
            except Exception as e:
                print(f"  -> Error deduplicating user '{username}': {e}")

    print(f"\nDeduplication complete. Processed {user_count} user(s). Removed {total_removed} duplicate entry/entries in total.")

if __name__ == "__main__":
    main()
