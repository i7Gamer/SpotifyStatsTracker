import datetime
import json
try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator

class Migrator(BaseMigrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def checkPreconditions(self):
        super().checkPreconditions()
        if self.tracksPath.exists() == False:
            raise FileExistsError("Tracks file doesn't exist. You might be on an older version.")

    def _getTimestamp(self, timestamp):
        if timestamp == "0000-00-00":
            return 0.0
        return datetime.datetime.strptime(timestamp, "%Y-%m-%d").timestamp()


    def migrate(self):
        users = [
            p.name
            for p in (self.baseDir / ".." / "Users").iterdir()
            if p.is_dir()
        ]
        for user in users:
            baseDir = self.baseDir / ".." / "Users" / user
            self.tracksPath =  baseDir / "tracks.json"
            self.checkPreconditions()

            with open(self.tracksPath, "r", encoding="utf-8") as f:
                tracks = json.load(f)

            for index, key in enumerate(tracks):
                tracks[key].pop("releaseDateText")
                tracks[key].pop("durationText")
                releaseDateText = tracks[key]["album"].pop("releaseDateText")
                ts = self._getTimestamp(releaseDateText)
                tracks[key]["album"]["releaseDate"] = ts
                tracks[key]["releaseDate"] = ts
                
                if "imageId" not in tracks[key]:
                    tracks[key]["imageId"] = tracks[key]["album"]["id"]
                    # tracks[key]["album"]["imageId"] = tracks[key]["album"]["id"]

                print(f"Processed {index+1}/{len(tracks)} entries", end="\r")

            with open(self.tracksPath, "w", encoding="utf-8") as f:
                json.dump(tracks, f, indent=4, ensure_ascii=False)
        
        self.updateAppVersion("1.5.0")



if __name__ == "__main__":
    migrator = Migrator("1.4.0", "1.5.0")
    migrator.migrate()

    print(
        f"Migration complete. "
        f"Created {result['entries']} entries and "
        f"{result['tracks']} unique tracks."
    )