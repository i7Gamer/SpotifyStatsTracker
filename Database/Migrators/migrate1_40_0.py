# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.40.0 -> 1.41.0: clears every track image marked 'failed' so the covers
    that 404'd against a malformed CDN URL can be fetched again.

    _imageUrlFromConnectMeta assumed the connect-state always carries the cover
    as 'spotify:image:<hash>'. When it carried an absolute URL instead, the
    rsplit(":") split the SCHEME, and the CDN prefix was glued onto the rest -
    producing https://i.scdn.co/image///i.scdn.co/image/<hash>, which can never
    resolve. The failed download marks the image 'failed', and _saveImg's claim
    gate treats that as permanent, so those albums would stay cover-less
    forever even now that the URL is built correctly.

    The attempted URL isn't stored anywhere, so poisoned rows can't be told
    apart from genuine 404s - all failed track images are cleared, as
    migrate1_20_0 did for artists. Over-reaching costs one retry; under-reaching
    costs a permanently blank cover."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            cleared = repo.deleteFailedTrackImages()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Cleared {cleared} failed track image(s) for retry under the fixed CDN URL builder.")
        self.updateAppVersion("1.41.0")


if __name__ == "__main__":
    Migrator("1.40.0", "1.41.0").migrate()
