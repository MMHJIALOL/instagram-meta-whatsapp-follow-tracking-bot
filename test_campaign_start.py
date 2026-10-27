"""Offline checks: campaign creation boundary, link recovery, and recording answers. No real messages."""
import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

os.environ.update(KEYWORD='*', IG_USER_ID='me', MEET_URL='')
import main
import dashboard
from fastapi.testclient import TestClient

main.DB = os.path.join(tempfile.mkdtemp(), 'test.db')
main.WS_TTL = 0
bot = TestClient(main.app)
dash = TestClient(dashboard.app, headers={'x-local': '1'})
start = main.now() - timedelta(minutes=5)
invite = 'https://chat.whatsapp.com/NEW31OCT'
spec = {'name': '31 Oct test', 'date': '2026-10-31', 'wa_group': '31st Diwali Special Workshop',
        'wa_link': invite, 'past_offer': False, 'started_at': start.isoformat(),
        'texts': {'RECORDING_MSG': 'Haan, recording available hogi. Details WhatsApp group mein milengi.'}}
with patch.object(dashboard, 'build', return_value={'ws_stats': {}}):
    bad = dash.post('/api/workshops', json={**spec, 'started_at': (main.now()+timedelta(days=1)).isoformat()})
    assert bad.status_code == 400
    assert dash.post('/api/workshops', json=spec).status_code == 200
assert main.workshop()['started'] == start
assert not main.current_event((start-timedelta(seconds=1)).timestamp()*1000)
assert main.current_event(start.timestamp()*1000)
assert main.current_event((start+timedelta(seconds=1)).isoformat())
assert not main.current_event('invalid')

sent = []
with patch.object(main, 'answer_dm', side_effect=lambda *args: sent.append(args)):
    for i, delta in enumerate([-1, 0, 1]):
        main.process({'entry': [{'id': 'me', 'messaging': [{'sender': {'id': 'viewer'},
            'timestamp': (start+timedelta(seconds=delta)).timestamp()*1000,
            'message': {'mid': f'boundary-{i}', 'text': 'Recording available hogi?', 'referral': {'ad_id': 'ad31'}}}]}]})
assert len(sent) == 2
hits = main.faq_matches('Recording available hogi?')
assert hits and 'recording available' in hits[0][1].lower()
with patch.object(main, 'api', return_value={'timestamp': (start-timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%S%z')}):
    assert not main.is_new('before-campaign')

code = main.personal_link('viewer').rsplit('/', 1)[1]
main.record_sent('viewer', 'test')
page = bot.get('/j/'+code, headers={'user-agent': 'Mozilla/5.0 Instagram 300'}, follow_redirects=False)
assert page.status_code == 200 and 'whatsapp://chat?code=NEW31OCT' in page.text
assert invite in page.text and 'Use WhatsApp Web' in page.text and 'Copy invite link' in page.text
assert page.headers['cache-control'].startswith('no-store')
with main.db() as conn:
    before = conn.execute('SELECT COUNT(*) FROM clicks').fetchone()[0]
    conn.execute('INSERT INTO used_links VALUES (?, ?, ?)', ('viewer', 'test-number', main.now().isoformat()))
used = bot.get('/j/'+code)
assert 'use ho chuka' in used.text and 'Open WhatsApp' in used.text
with main.db() as conn:
    assert conn.execute('SELECT COUNT(*) FROM clicks').fetchone()[0] == before
assert bot.get('/j/unknown').status_code == 404
print('PASS: exact start boundary, old events ignored, recording FAQ, phone/browser/copy fallbacks, recovery, no-cache, unknown links')
