from pathlib import Path
import importlib.util

try:
    from Database.Migrators.base import resolveRuntimeDir, BaseMigrator
except ModuleNotFoundError:
    from base import resolveRuntimeDir, BaseMigrator

def _import(name, modulePath):
    spec = importlib.util.spec_from_file_location(name, modulePath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def migrate(major, minor, baseDir):
    print(f"Migrating from version {major}.{minor}.0")

    # Migrator module names encode the major version too (not hardcoded to
    # "1") so a future major-version bump's migrators (e.g. migrate2_0_0.py)
    # get picked up correctly instead of always looking for a "migrate1_*"
    # file regardless of which major version the database is actually on.
    moduleName = f"migrate{major}_{minor}_0"
    modulePath = baseDir / f"{moduleName}.py"
    module = _import(moduleName, modulePath)

    fromVersion = f"{major}.{minor}.0"
    toVersion = f"{major}.{minor + 1}.0"
    Migrator = module.Migrator
    Migrator(fromVersion, toVersion).migrate()

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

    # Compare the full (major, minor) pair, not just the minor component -
    # otherwise a database and app that only differ in major version (e.g.
    # "1.7.0" vs "2.7.0") would be mistaken for already being up to date, and
    # a genuine major bump would make the loop below hunt forever for a
    # migrator file that can never satisfy a minor-only comparison.
    while BaseMigrator.getMajorMinor(databaseVersion) != BaseMigrator.getMajorMinor(appVersion):
        dbMajor, dbMinor = BaseMigrator.getMajorMinor(databaseVersion)
        migrate(dbMajor, dbMinor, baseDir)

        databaseVersionFile = resolveRuntimeDir(baseDir) / "VERSION"   #< location may have changed (e.g. a Users/ -> Data/ rename)
        databaseVersion = databaseVersionFile.read_text().strip()