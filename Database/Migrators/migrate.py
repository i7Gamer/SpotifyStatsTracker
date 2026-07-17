from pathlib import Path
import importlib.util

try:
    from Database.Migrators.base import resolveRuntimeDir, BaseMigrator
    from Database.Migrators import dbversion
except ModuleNotFoundError:
    from base import resolveRuntimeDir, BaseMigrator
    import dbversion

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

def _resolveDatabaseVersion(runtimeDir: Path) -> str | None:
    """The current database version, or None if this is a genuinely fresh
    install (nothing to migrate). The version lives inside spotify_stats.db
    itself (schema_version table) once one exists - it then survives a raw
    file copy, which is how Database/backup.py snapshots the database and
    exactly the scenario a sibling VERSION file gets desynced by (a backup
    predating a later migration, restored after the sibling file has since
    moved on).

    A database that predates the schema_version table falls back to the
    sibling file, backfilling the in-db marker so the next read (and the
    next backup) carries it. A database with real data but no marker
    anywhere - an orphaned file, or a backup restored without its VERSION
    file - is refused rather than guessed at: several historical migrations
    (data-only cleanups, in-place encryption) leave no structural trace a
    version could be reliably inferred from, so a silent wrong guess risks
    silently skipping a migration the data actually needs."""
    dbPath = runtimeDir / "spotify_stats.db"
    databaseVersionFile = runtimeDir / "VERSION"

    if dbPath.exists():
        dbVersion = dbversion.readDbVersion(dbPath)
        if dbVersion is not None:
            return dbVersion
        if databaseVersionFile.exists():
            version = databaseVersionFile.read_text().strip()
            dbversion.writeDbVersion(dbPath, version)
            return version
        if dbversion.hasAnyData(dbPath):
            raise RuntimeError(
                f"{dbPath} has data but no version marker, either inside the "
                "database or in a sibling VERSION file - refusing to guess. "
                "If this is a restored backup, restore its VERSION file "
                "alongside it, or set the version explicitly via "
                "Database.Migrators.dbversion.writeDbVersion()."
            )
        return None   #< empty db, no marker anywhere - fresh install
    if databaseVersionFile.exists():
        return databaseVersionFile.read_text().strip()
    return None   #< pre-database (JSON-history) era, nothing on disk yet - fresh install


def migrateIfNeeded():
    baseDir = Path(__file__).resolve().parent
    appVersionFile = baseDir / ".." / "VERSION"
    appVersion = appVersionFile.read_text().strip()

    runtimeDir = resolveRuntimeDir(baseDir)
    databaseVersion = _resolveDatabaseVersion(runtimeDir)
    if databaseVersion is None:
        runtimeDir.mkdir(parents=True, exist_ok=True)   #< runtime data dir absent on a fresh install
        (runtimeDir / "VERSION").write_text(appVersion)
        dbPath = runtimeDir / "spotify_stats.db"
        if dbPath.exists():
            dbversion.writeDbVersion(dbPath, appVersion)
        return   #< means this is first run, no migration needed

    # Compare the full (major, minor) pair, not just the minor component -
    # otherwise a database and app that only differ in major version (e.g.
    # "1.7.0" vs "2.7.0") would be mistaken for already being up to date, and
    # a genuine major bump would make the loop below hunt forever for a
    # migrator file that can never satisfy a minor-only comparison.
    while BaseMigrator.getMajorMinor(databaseVersion) != BaseMigrator.getMajorMinor(appVersion):
        dbMajor, dbMinor = BaseMigrator.getMajorMinor(databaseVersion)
        migrate(dbMajor, dbMinor, baseDir)

        runtimeDir = resolveRuntimeDir(baseDir)   #< location may have changed (e.g. a Users/ -> Data/ rename)
        databaseVersion = _resolveDatabaseVersion(runtimeDir)