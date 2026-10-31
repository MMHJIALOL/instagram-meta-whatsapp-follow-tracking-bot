import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import main
import dashboard
from fastapi.testclient import TestClient

class HistoryDashboardTests(unittest.TestCase):
    def test_repair_restarts_only_listener_and_resumes_sharing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DB', str(Path(tmp)/'test.db')):
            dashboard.set_meta('wa_history_settings', {'enabled': False, 'retry': 0})
            procs = {'listener': [123], 'members_keepalive': [456]}
            def connect(_):
                dashboard.set_meta('wa', {'state': 'ready'})
            with patch.object(dashboard, 'processes', return_value=procs), \
                 patch.object(dashboard, 'kill') as kill, \
                 patch.object(dashboard, 'set_autostart') as autostart, \
                 patch.object(dashboard, 'start_detached') as launch, \
                 patch.object(dashboard.time, 'sleep', side_effect=connect):
                dashboard._history_repair.update(running=True)
                dashboard.repair_history()
                kill.assert_called_once_with([123])
                autostart.assert_called_once_with('members', True)
                launch.assert_not_called()
            settings = dashboard.get_meta('wa_history_settings')
            self.assertTrue(settings['enabled'])
            self.assertGreater(settings['retry'], 0)
            self.assertTrue(dashboard._history_repair['ok'])
            self.assertFalse(dashboard._history_repair['running'])
            gc.collect()

    def test_repair_starts_missing_supervisor_and_reports_link_required(self):
        values = {'wa_history_settings': {}, 'wa': {'state': 'qr'}}
        with patch.object(dashboard, 'get_meta', side_effect=lambda key, default=None: values.get(key, default)), \
             patch.object(dashboard, 'set_meta'), patch.object(dashboard, 'set_autostart'), \
             patch.object(dashboard, 'processes', return_value={'listener': [], 'members_keepalive': []}), \
             patch.object(dashboard, 'kill'), patch.object(dashboard, 'start_detached') as launch:
            dashboard._history_repair.update(running=True)
            dashboard.repair_history()
            self.assertIn('members_keepalive.py', launch.call_args.args[0])
            self.assertFalse(dashboard._history_repair['ok'])
            self.assertIn('QR', dashboard._history_repair['step'])
            self.assertFalse(dashboard._history_repair['running'])

    def test_repair_button_deduplicates_requests(self):
        client = TestClient(dashboard.app, headers={'x-local': '1'})
        dashboard._history_repair.update(running=False)
        try:
            with patch.object(dashboard.threading, 'Thread') as thread:
                self.assertTrue(client.post('/api/wa/history/repair').json()['started'])
                self.assertFalse(client.post('/api/wa/history/repair').json()['started'])
                thread.assert_called_once_with(target=dashboard.repair_history, daemon=True)
                thread.return_value.start.assert_called_once()
        finally:
            dashboard._history_repair.update(running=False)

    def test_controls_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=Path(tmp)/'jobs.json'
            jobs.write_text(json.dumps({'a':{'chat':'Test community','id':'one','t':1,'done':True,'last':json.dumps({'result':'sent','count':3})},'b':{'chat':'Test community','id':'two','t':2,'done':False},'c':{'chat':'Other','id':'three','t':3,'done':False}}))
            with patch.object(main,'DB',str(Path(tmp)/'test.db')),patch.object(dashboard,'HISTORY_JOBS',jobs),patch.object(dashboard,'main_group',return_value='Test community'):
                c=TestClient(dashboard.app,headers={'x-local':'1'})
                self.assertTrue(c.get('/api/wa/history/settings').json()['enabled'])
                self.assertEqual(c.post('/api/wa/history/settings',json={'enabled':'false'}).status_code,400)
                self.assertFalse(c.post('/api/wa/history/settings',json={'enabled':False}).json()['enabled'])
                self.assertFalse(TestClient(dashboard.app,headers={'x-local':'1'}).get('/api/wa/history/settings').json()['enabled'])
                self.assertEqual(c.post('/api/wa/history/retry').status_code,400)
                c.post('/api/wa/history/settings',json={'enabled':True})
                self.assertEqual(c.post('/api/wa/history/retry').status_code,200)
                self.assertGreater(c.get('/api/wa/history/settings').json()['retry'],0)
                data=c.get('/api/wa/history').json()
                self.assertEqual(data['counts'],{'sent':1,'already_received':0,'pending':1,'skipped':0})
                self.assertEqual(len(data['jobs']),2)
                jobs.write_text('broken')
                self.assertIsNotNone(c.get('/api/wa/history').json()['error'])
            gc.collect()

if __name__=='__main__': unittest.main()
