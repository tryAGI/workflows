import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

WORKFLOW = Path(os.environ.get('AUTO_MERGE_WORKFLOW', str(Path(__file__).resolve().parents[1] / '.github/workflows/auto-merge.yml')))


class ValidationChecksTests(unittest.TestCase):
    def run_checks(self, responses):
        workflow = WORKFLOW.read_text()
        match = re.search(r'      - name: Wait for validation checks\n.*?        run: \|\n(.*?)(?=\n      - name:)', workflow, re.S)
        self.assertIsNotNone(match)
        script = '\n'.join(line[10:] for line in match[1].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'responses.json').write_text(json.dumps(responses))
            (root / 'gh').write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
root=Path(os.environ['MOCK_ROOT'])
count=int((root/'count').read_text()) if (root/'count').exists() else 0
responses=json.loads((root/'responses.json').read_text())
r=responses[min(count,len(responses)-1)]
(root/'count').write_text(str(count+1))
sys.stdout.write(r.get('stdout',''))
sys.stderr.write(r.get('stderr',''))
sys.exit(r['code'])
''')
            (root / 'sleep').write_text('#!/bin/sh\nexit 0\n')
            for name in ['gh', 'sleep']:
                (root / name).chmod(0o755)
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'], MOCK_ROOT=str(root), PR_URL='https://github.com/tryAGI/example/pull/1')
            result = subprocess.run(['bash','-e','-o','pipefail','-c',script], env=env, capture_output=True,text=True,timeout=15)
            count = int((root/'count').read_text()) if (root/'count').exists() else 0
            return result,count

    @staticmethod
    def response(bucket='pass', code=0, workflow='CI'):
        return {'code':code,'stdout':json.dumps([{'bucket':bucket,'name':'build','workflow':workflow}])}

    def test_passing_independent_checks(self):
        result,count=self.run_checks([self.response()])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(count,1)

    def test_pending_exit_eight_then_success(self):
        result,count=self.run_checks([self.response('pending',8),self.response()])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(count,2)

    def test_failed_and_cancelled_checks_stop(self):
        for bucket in ['fail','cancel']:
            with self.subTest(bucket=bucket):
                result,count=self.run_checks([self.response(bucket,1)])
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(count,1)

    def test_query_error_is_visible_and_stops_immediately(self):
        result,count=self.run_checks([{'code':1,'stdout':'','stderr':'Resource not accessible by integration\n'}])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(count,1,result.stderr)
        self.assertIn('Resource not accessible by integration',result.stderr)
        self.assertNotIn('integer expression expected',result.stderr)

    def test_invalid_responses_stop_immediately(self):
        for output in ['', '   ', '[] []', 'not json', '{}', '[null]', '[{"bucket":"unknown","workflow":"CI","name":"build"}]']:
            with self.subTest(output=output):
                result,count=self.run_checks([{'code':0,'stdout':output}])
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(count,1)

    def test_unexpected_cli_exit_does_not_authorize_merge(self):
        result,count=self.run_checks([self.response(code=2)])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(count,1)

    def test_no_independent_checks_times_out(self):
        result,count=self.run_checks([{'code':0,'stdout':'[]'}])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(count,30)

    def test_self_checks_do_not_authorize_merge(self):
        result,count=self.run_checks([self.response(workflow='Auto-approve and auto-merge bot pull requests')])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(count,30)

    def test_skipped_only_checks_do_not_authorize_merge(self):
        result,count=self.run_checks([self.response('skipping')])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(count,1)


if __name__=='__main__':
    unittest.main()
