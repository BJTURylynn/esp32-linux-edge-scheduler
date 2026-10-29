import csv,json,tempfile,unittest
from pathlib import Path
from tools.dual_verify import verify
from tools.protocol import reference


class DualVerificationTests(unittest.TestCase):
    def fixture(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);capture=root/'capture';service=root/'service'
        capture.mkdir();service.mkdir()
        (capture/'summary.json').write_text(json.dumps({'success':True,'requested_each':1}))
        samples=[i%201-100 for i in range(256)]
        rows=[]
        for source,device in [('arduino','uno-01'),('esp32','esp32c3-01')]:
            (capture/source).mkdir()
            message=dict(device_id=device,seq=0,source='synthetic',priority='normal',samples=samples)
            (capture/source/'frames.jsonl').write_text(json.dumps(message)+'\n')
            rows.append(dict(device_id=device,seq=0,source='synthetic',priority='normal',samples=256,status='completed',**reference(samples,8)))
        self.write_rows(service,rows)
        state=dict(accepted=2,completed=2,rejected=0,window=8,passes=1,fatal=False)
        (service/'status.json').write_text(json.dumps(state))
        return capture,service,rows,state

    @staticmethod
    def write_rows(service,rows):
        with (service/'tasks.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['device_id','seq','source','priority','samples','status','rms','mean'])
            writer.writeheader();writer.writerows(rows)

    def test_equal_sequences_across_devices_are_valid(self):
        capture,service,rows,state=self.fixture()
        result=verify(capture,service)
        self.assertTrue(result['verified'])
        self.assertEqual(result['devices'],{'uno-01':1,'esp32c3-01':1})

    def test_empty_batch_cannot_be_verified(self):
        capture,service,rows,state=self.fixture()
        (capture/'summary.json').write_text(json.dumps({'success':True,'requested_each':0}))
        for source in ('arduino','esp32'):(capture/source/'frames.jsonl').write_text('')
        self.write_rows(service,[])
        (service/'status.json').write_text(json.dumps(dict(state,accepted=0,completed=0)))
        with self.assertRaises(ValueError):verify(capture,service)

    def test_corrupted_computed_result_is_rejected(self):
        capture,service,rows,state=self.fixture()
        rows[0]['rms']+=1
        self.write_rows(service,rows)
        with self.assertRaises(ValueError):verify(capture,service)

    def test_duplicate_identity_does_not_hide_missing_device(self):
        capture,service,rows,state=self.fixture()
        rows[1]['device_id']='uno-01'
        self.write_rows(service,rows)
        with self.assertRaises(ValueError):verify(capture,service)

    def test_fatal_service_is_not_reported_as_success(self):
        capture,service,rows,state=self.fixture()
        (service/'status.json').write_text(json.dumps(dict(state,fatal=True)))
        with self.assertRaises(ValueError):verify(capture,service)


if __name__=='__main__':unittest.main()
