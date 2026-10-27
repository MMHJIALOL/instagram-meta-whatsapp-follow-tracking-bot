"""Offline integration checks: no real API calls, messages, or database changes."""
import os
import gc
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("IG_USER_ID", "me")
import main
import dashboard
from fastapi.testclient import TestClient


class BypassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(main, "DB", os.path.join(self.tmp.name, "test.db"))
        self.db_patch.start()
        self.client = TestClient(dashboard.app, headers={"x-local": "1"})
        self.seq = 0
        self.api = patch.object(main, "api", return_value={"data": [
            {"id": "222", "permalink": "https://www.instagram.com/reel/ABC/"}]}).start()
        self.handle = patch.object(main, "handle", return_value=False).start()
        self.answer = patch.object(main, "answer_dm").start()
        patch.object(main, "is_new", return_value=True).start()
        self.add()

    def tearDown(self):
        patch.stopall()
        gc.collect()
        self.tmp.cleanup()

    def add(self):
        return self.client.post('/api/bypassed-media', json={"url": "instagram.com/reel/ABC/?igsh=123"})

    def event(self, **entry):
        main.process({"entry": [{"id": "me", **entry}]})

    def comment(self, media="222", who="u"):
        self.seq += 1
        self.event(changes=[{"field": "comments", "value": {"id": f"c{self.seq}",
            "text": "workshop", "from": {"id": who}, "media": {"id": media}}}])

    def dm(self, **extra):
        self.seq += 1
        self.event(messaging=[{"sender": {"id": "u"}, "message": {
            "mid": f"m{self.seq}", "text": "DONE", **extra}}])

    def test_crud_validation_and_duplicates(self):
        self.assertEqual(self.add().status_code, 200)
        rows = self.client.get('/api/bypassed-media').json()['items']
        self.assertEqual(rows, [{"media_id": "222", "url": "https://www.instagram.com/reel/ABC/"}])
        for url in ["", "https://evilinstagram.com/reel/ABC/", "javascript:alert(1)"]:
            self.assertEqual(self.client.post('/api/bypassed-media', json={"url": url}).status_code, 400)
        self.client.delete('/api/bypassed-media/222')
        self.assertEqual(main.bypassed_media(), {})

    def test_comments_followups_buttons_and_new_source(self):
        self.comment()
        self.dm()
        self.event(messaging=[{"sender": {"id": "u"}, "postback": {"mid": "p1", "payload": main.CHECK_FOLLOW}}])
        self.handle.assert_not_called()
        self.answer.assert_not_called()
        with main.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)
        self.comment('333')
        self.handle.assert_called_once()
        self.dm()
        self.answer.assert_called_once()

    def test_direct_dm_sources_and_unrelated_text(self):
        self.dm(referral={"media_id": "222"})
        self.answer.assert_not_called()
        self.client.delete('/api/bypassed-media/222')
        self.dm()
        self.answer.assert_called_once()
        self.add()
        main.set_media_source('u', '')
        self.answer.reset_mock()
        self.dm(attachments=[{"type": "share", "payload": {"url": "https://www.instagram.com/reel/ABC/?x=1"}}])
        self.answer.assert_not_called()
        self.dm(referral={"media_id": "333"})
        self.answer.assert_called_once()

    def test_sends_and_existing_reel_context_are_suppressed(self):
        with main.db() as conn:
            conn.execute("INSERT INTO reels (id, media_id) VALUES (1, '222')")
            conn.execute("INSERT INTO funnel VALUES ('u', 1, '')")
        self.assertTrue(main.bypassed_person('u'))
        self.api.reset_mock()
        self.assertFalse(main.send({"id": "u"}, 'reminder'))
        self.api.assert_not_called()
        main.set_media_source('u', '333')
        self.assertFalse(main.bypassed_person('u'))


if __name__ == '__main__':
    unittest.main()
