import json

try:
    from Database.Migrators.base import BaseMigrator
    from Database.database import Database
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator
    from database import Database

class Migrator(BaseMigrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def checkPreconditions(self):
        super().checkPreconditions()
        if self.tracksPath.exists() == False:
            raise FileExistsError("Tracks file doesn't exist. You might be on an older version.")

    def _loadJsonFile(self, path, default) -> list:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(default, indent=4), encoding="utf-8")
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.write_text(json.dumps(default, indent=4), encoding="utf-8")
            return default

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
            metadataFolder = baseDir / "img" / "tracks"# / "metadata.json"
            downloadedImages = list(metadataFolder.iterdir())
            db = Database(user)

            for index, key in enumerate(tracks.keys()):
                imageId = tracks[key]["imageId"]

                downloadedIds = [p.stem for p in downloadedImages]

                if imageId not in downloadedIds:
                    print(f"Renaming image for {tracks[key]['name']}.")

                    oldPath = metadataFolder / f"{tracks[key]['id']}.jpeg"
                    newPath = metadataFolder / f"{imageId}.jpeg"

                    if oldPath.exists():
                        print(f"{oldPath} -> {newPath}")
                        oldPath.rename(newPath)
                        downloadedImages.append(newPath)
                    else:
                        db.saveTrackImg(tracks[key]["imageUrl"], imageId)   #< Should not use imported db but im too lazy to change
                        print(f"Missing file: {oldPath}")

                # Fix artist image IDs
                for i in range(len(tracks[key]["artists"])):
                    tracks[key]["artists"][i]["imageId"] = tracks[key]["artists"][i]["id"]

                print(f"Processed {index+1}/{len(tracks)} entries", end="\r")


            with open(self.tracksPath, "w", encoding="utf-8") as f:
                json.dump(tracks, f, indent=4, ensure_ascii=False)

        self.updateAppVersion("1.6.0")



if __name__ == "__main__":
    migrator = Migrator()
    result = migrator.migrate()

    print(
        f"Migration complete. "
        f"Created {result['entries']} entries and "
        f"{result['tracks']} unique tracks."
    )