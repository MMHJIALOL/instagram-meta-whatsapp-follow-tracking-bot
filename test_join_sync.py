"""Offline integration: atomic click import and replay safety."""
from contextlib import closing
import os
import tempfile
import unittest
from unittest.mock import patch
import main
import join_sync


class JoinSyncTests(unittest.TestCase):
    def test_import_and_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(main, 'DB', os.path.join(folder, 'test.db')), patch.dict(os.environ, {'JOIN_SERVICE_BASE':'https://test.example'}):
                with closing(main.db()) as conn, conn:
                    conn.execute('INSERT INTO links VALUES (?,?,?)', ('test123','person','2026-10-27'))
                events=[{'id':1,'code':'test123','at':'2026-10-27T00:00:00+00:00','ua':'Mobile Instagram test','ip':'203.0.113.1'}]
                def fake(path):
                    return {'events': events if path.endswith('=0') else []}
                with patch.object(join_sync, 'api', side_effect=fake):
                    self.assertEqual(join_sync.pull_events(main),1)
                    self.assertEqual(join_sync.pull_events(main),0)
                with closing(main.db()) as conn, conn:
                    self.assertEqual(conn.execute('SELECT igsid,COUNT(*) FROM clicks').fetchone(),('person',1))
                    self.assertEqual(conn.execute('SELECT cursor FROM join_sync_state').fetchone()[0],1)


if __name__ == '__main__': unittest.main()
