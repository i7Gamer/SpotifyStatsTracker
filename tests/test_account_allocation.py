"""Registration and display-name updates reserve the same identity namespace."""
import threading
from pathlib import Path
from unittest.mock import patch

from _app_factory import AppTestCase
from test_repository import RepositoryTestCase, makeTrack
from Database.repository import Repository

THREAD_DEADLINE_SECONDS = 5


class TestAccountAllocation(AppTestCase):
    def test_case_and_display_collisions_are_suffixed(self):
        dash = self._makeApp()
        dash.repo.upsertUser("TrustedOwner", "owner@example.test")
        dash.repo.upsertUser("another", "another@example.test")
        assert dash.repo.setDisplayName("another", "trustedowner_1")
        assert dash.get_or_create_user("trustedowner@new.example") == "trustedowner_2"
        assert dash.get_or_create_user("OWNER@example.test") == "TrustedOwner"

    def test_orphan_privileges_and_history_remain_with_the_owner(self):
        dash = self._makeApp()
        repo = dash.repo
        repo.upsertUser("legacy", None)
        repo.upsertTrack(makeTrack())
        repo.insertPlay("legacy", "t1", 1000, 10000)
        repo._conn().execute("UPDATE users SET is_admin=1 WHERE username='legacy'")
        repo.commit()
        before = dict(repo._conn().execute("SELECT * FROM users WHERE username='legacy'").fetchone())
        assert dash.get_or_create_user("legacy@attacker.example") == "legacy_1"
        after = dict(repo._conn().execute("SELECT * FROM users WHERE username='legacy'").fetchone())
        assert after == before
        assert repo._conn().execute("SELECT username FROM plays").fetchone()[0] == "legacy"

    def test_rename_before_allocation_is_rechecked_at_the_insert(self):
        dash = self._makeApp()
        dash.repo.upsertUser("existing", "existing@example.test")
        allocate = dash.repo.createUserIfNameAvailable

        def rename_then_allocate(username, email):
            if username == "candidate":
                assert dash.repo.setDisplayName("existing", username)
            return allocate(username, email)

        with patch.object(dash.repo, "createUserIfNameAvailable", side_effect=rename_then_allocate):
            assert dash.get_or_create_user("candidate@example.test") == "candidate_1"


class TestAtomicNameReservation(RepositoryTestCase):
    def test_available_name_insert_and_conflicts(self):
        assert self.repo.createUserIfNameAvailable("first", "first@example.test")
        assert not self.repo.createUserIfNameAvailable("FIRST", "second@example.test")
        assert self.repo.getEmailForUsername("first") == "first@example.test"
        assert self.repo.setDisplayName("first", "Second")
        assert not self.repo.createUserIfNameAvailable("second", "second@example.test")
        assert self.repo.createUserIfNameAvailable("third", "third@example.test")
        assert not self.repo.setDisplayName("first", "THIRD")

    def test_concurrent_rename_wins_before_waiting_allocation(self):
        self.repo.upsertUser("existing", "existing@example.test")
        conn = self.repo._conn()
        attempted = threading.Event()
        ready = threading.Event()
        start_allocation = threading.Event()
        outcome = []
        database_path = Path(self._tmpdir.name) / "test.db"

        def allocate():
            other = Repository(database_path)
            try:
                other._conn().set_trace_callback(
                    lambda sql: attempted.set() if sql.lstrip().upper().startswith("INSERT INTO USERS") else None)
                ready.set()
                assert start_allocation.wait(THREAD_DEADLINE_SECONDS)
                outcome.append(other.createUserIfNameAvailable("claimed", "new@example.test"))
            except Exception as error:
                outcome.append(error)
            finally:
                other.connectionManager.close()

        thread = threading.Thread(target=allocate)
        thread.start()
        try:
            assert ready.wait(THREAD_DEADLINE_SECONDS)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE users SET display_name='claimed' WHERE username='existing'")
            start_allocation.set()
            assert attempted.wait(THREAD_DEADLINE_SECONDS)
        finally:
            start_allocation.set()
            conn.commit()
            thread.join(THREAD_DEADLINE_SECONDS)
        assert not thread.is_alive()
        assert outcome == [False]
