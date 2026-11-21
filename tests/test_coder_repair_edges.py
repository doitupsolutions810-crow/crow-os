"""Bounded coder repair keeps capabilities, evidence and acceptance gates intact."""
import tempfile
import unittest
from pathlib import Path

from crow_os.capabilities import Broker
from crow_os.ledger import Ledger
from crow_os.llm import LLMUnavailable
from crow_os.worker import LLMWorker
from crow_os.workflow import Workflow


class ScriptedClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def chat_stream(self, system, user, on_token=None):
        index = len(self.calls)
        self.calls.append((system, user))
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        response = response(user) if callable(response) else response
        if on_token:
            on_token(response)
        return response


def valid_artifact(user):
    ref = user.rsplit('Plan reference line: ', 1)[-1].strip()
    return '# ' + ref + '\ndef add(a,b): return a+b\n'


class BoundedCoderRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.jsonl')
        self.broker = Broker(b'coder-repair-test-secret-32-bytes!')
        self.workflow = Workflow(self.ledger)
        self.cap = self.broker.issue('kernel', [
            {'method': 'EXECUTE', 'resource': 'jobs.debugger'},
            {'method': 'EXECUTE', 'resource': 'jobs.coder'},
        ], rate_limit=10)
        self.outputs = {}

    def run_script(self, responses):
        client = ScriptedClient(responses)
        worker = LLMWorker(self.broker, self.workflow, self.ledger, client)
        result = worker.run('add two numbers', self.cap, outputs=self.outputs)
        return result, client

    def repairs(self):
        return [e for e in self.ledger.read() if e['kind'] == 'coder.repair']

    def test_valid_first_artifact_needs_no_repair(self):
        result, client = self.run_script(['1. add numbers', valid_artifact])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.repairs(), [])
        self.assertTrue(self.ledger.verify()['ok'])

    def test_unfixed_artifact_stops_after_one_repair(self):
        result, client = self.run_script(['1. add', 'def add(a,b): return a+b', 'still missing reference'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(self.repairs()), 1)
        failed = [e for e in self.ledger.read() if e['kind'] == 'job.failed']
        self.assertEqual(failed[-1]['raw']['role'], 'tester')
        self.assertIn('missing plan reference line', failed[-1]['raw']['note'])
        self.assertEqual(self.outputs[result['job_id']]['artifact'], 'still missing reference')
        self.assertFalse(any(e['kind'] == 'job.step' and e['raw']['role'] == 'governance'
                             for e in self.ledger.read()))

    def test_revoked_coder_capability_prevents_repair_model_call(self):
        def revoke_then_return(user):
            self.broker.revoke(self.cap['id'])
            return 'missing reference'
        result, client = self.run_script(['1. add', revoke_then_return])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.repairs(), [])
        self.assertIn('CapabilityError', result['error'])

    def test_provider_failure_during_repair_keeps_draft_evidence(self):
        draft = 'def add(a,b): return a+b'
        result, client = self.run_script(['1. add', draft, LLMUnavailable('provider unavailable')])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(client.calls), 3)
        self.assertIn('LLMUnavailable', result['error'])
        self.assertEqual(self.outputs[result['job_id']]['artifact'], draft)
        self.assertEqual(self.outputs[result['job_id']]['plan'], '1. add')
        self.assertEqual(len(self.repairs()), 1)

    def test_embedded_reference_is_repaired_instead_of_auto_accepted(self):
        def embedded(user):
            ref = user.rsplit('Plan reference line: ', 1)[-1].strip()
            return 'unrelated-' + ref + '-suffix'
        result, client = self.run_script(['1. add', embedded, valid_artifact])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(self.repairs()), 1)
        self.assertTrue(self.ledger.verify()['ok'])
