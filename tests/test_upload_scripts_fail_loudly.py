"""The Docker publish scripts must exit non-zero when they refuse to publish.

Both scripts print "Aborting push" and then run off the end of the file, so the
process exits 0 - "success" - after deciding not to publish anything. PowerShell
also does not stop on a failed native command, so a `docker build` that failed
used to fall straight through to `docker tag`, which happily re-tags whatever
older image still carries that name. The visible outcome of a broken build was
therefore a *stale* image being pushed, reported as a clean run.

Nothing here runs docker; these are file-level assertions on the two scripts,
in the same spirit as tests/test_dockerignore_excludes_private_data.py.
"""
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = ("uploadDocker.ps1", "uploadDockerNightly.ps1")

# The publish-blocking decisions each script makes. Each must be followed by a
# non-zero exit rather than by falling through to the end of the file.
_BUILD_MARKER = "docker build"
_TAG_MARKER = "docker tag"
_ABORT_MARKER = "Aborting push"

_EXIT_NONZERO = re.compile(r"\bexit\s+[1-9]\d*\b")
# The `docker run` COMMAND, not a mention of it: the comments around it name the
# command too, and a substring search finds the prose first.
_DOCKER_RUN_LINE = re.compile(r"^docker run\b", re.MULTILINE)
_LASTEXITCODE = re.compile(r"\$LASTEXITCODE")
# A non-zero exit that only fires under a condition. An UNconditional one at
# the end of the file would fail the successful run too.
_GUARDED_EXIT_NONZERO = re.compile(r"if\s*\([^)]*\)\s*\{[^}]*?\bexit\s+[1-9]\d*\b", re.DOTALL)


def _read(name):
    return (_ROOT / name).read_text(encoding="utf-8")


class TestUploadScriptsFailLoudly(unittest.TestCase):
    def test_deciding_not_to_push_ends_in_a_non_zero_exit(self):
        """Not asserted *inside* the abort branch on purpose: `docker logs` and
        `docker rm` are the diagnostic for a crashed container and the cleanup
        for the test container, so both have to run first. The exit therefore
        belongs at the end of the script, guarded by whether a push actually
        happened."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                source = _read(name)
                abortAt = source.index(_ABORT_MARKER)
                tail = source[source.index("docker rm"):]

                self.assertNotIn("docker push", source[abortAt:],
                                 f"{name} still pushes after announcing an abort")
                self.assertIsNotNone(
                    _GUARDED_EXIT_NONZERO.search(tail),
                    f"{name} runs off the end of the file, so a refused publish still exits 0",
                )

    def test_the_final_exit_is_conditional(self):
        """An unconditional `exit 1` at the end would 'fix' the abort path by
        failing the successful publish too."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                tail = _read(name)[_read(name).index("docker rm"):]
                stripped = _GUARDED_EXIT_NONZERO.sub("", tail)

                self.assertIsNone(_EXIT_NONZERO.search(stripped),
                                  f"{name} has a non-zero exit outside any condition")

    def test_a_failed_build_is_not_tagged_and_pushed(self):
        """The dangerous ordering: `docker tag` immediately follows
        `docker build`, and a failed build leaves the previous image under that
        name. Without an exit-code check in between, "the build broke" and "we
        published yesterday's image again" look identical."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                source = _read(name)
                buildAt = source.index(_BUILD_MARKER)
                tagAt = source.index(_TAG_MARKER)
                between = source[buildAt:tagAt]

                self.assertIsNotNone(_LASTEXITCODE.search(between),
                                     f"{name} never checks whether the build succeeded")
                self.assertIsNotNone(_EXIT_NONZERO.search(between),
                                     f"{name} checks the build but does not abort on failure")

    def test_the_test_container_start_is_verified(self):
        """`docker run --name test-tracker` FAILS when that name is already
        taken, and the exit code was never checked - so `docker inspect
        test-tracker` then reported on the LEFTOVER container instead. A run
        interrupted between `docker run` and `docker rm` leaves one behind (and
        both scripts share the single fixed name, so they collide with each
        other too): if that stale container happened to be running, the script
        announced "Container is stable" and pushed an image it never started."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                source = _read(name)
                between = source[_DOCKER_RUN_LINE.search(source).start():source.index("docker inspect")]

                self.assertIsNotNone(_LASTEXITCODE.search(between),
                                     f"{name} never checks whether the test container started")
                self.assertIsNotNone(_EXIT_NONZERO.search(between),
                                     f"{name} checks the container start but does not abort on failure")

    def test_a_leftover_test_container_is_cleared_first(self):
        """The other half: aborting on a name clash would turn every interrupted
        run into a manual cleanup chore, so the name is reclaimed up front."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                source = _read(name)
                beforeRun = source[:_DOCKER_RUN_LINE.search(source).start()]

                self.assertIn("docker rm -f", beforeRun,
                              f"{name} does not clear a leftover test container before starting one")

    def test_the_startup_check_still_runs_before_any_push(self):
        """Guard on the guard: the container smoke test has to keep happening
        between build and push, or the exit codes above are protecting nothing."""
        for name in _SCRIPTS:
            with self.subTest(script=name):
                source = _read(name)
                self.assertLess(source.index("docker run"), source.index("docker push"))
                self.assertLess(source.index("$isRunning"), source.index("docker push"))


if __name__ == "__main__":
    unittest.main()
