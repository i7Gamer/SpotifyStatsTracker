from pathlib import Path
import importlib.util

try:
    from Database.Migrators.base import resolveRuntimeDir
except ModuleNotFoundError:
    from base import resolveRuntimeDir

def getMiddleVersion(version):
    return int(version.split(".")[1])

def _import(name, modulePath):
    spec = importlib.util.spec_from_file_location(name, modulePath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def migrate(version, baseDir):
    print(f"Migrating from version 1.{version}.0")

    modulePath = baseDir / f"migrate1_{version}_0.py"
    module = _import(f"migrate1_{version}_0", modulePath)

    Migrator = module.Migrator
    Migrator().migrate()

def migrateIfNeeded():
    baseDir = Path(__file__).resolve().parent
    appVersionFile = baseDir / ".." / "VERSION"
    appVersion = appVersionFile.read_text().strip()

    databaseVersionFile = resolveRuntimeDir(baseDir) / "VERSION"
    if databaseVersionFile.exists() == False:
        databaseVersionFile.parent.mkdir(parents=True, exist_ok=True)   #< runtime data dir absent on a fresh install
        databaseVersionFile.write_text(appVersion)
        return   #< means this is first run, no migration needed
    databaseVersion = databaseVersionFile.read_text().strip()

    while getMiddleVersion(databaseVersion) != getMiddleVersion(appVersion):
        dbVersion = getMiddleVersion(databaseVersion)
        migrate(dbVersion, baseDir)

        databaseVersionFile = resolveRuntimeDir(baseDir) / "VERSION"   #< location may have changed (e.g. a Users/ -> Data/ rename)
        databaseVersion = databaseVersionFile.read_text().strip()