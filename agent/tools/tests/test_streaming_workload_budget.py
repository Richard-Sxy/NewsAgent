"""Host-stdlib tests: no Docker calls or Windows memory APIs are invoked."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from streaming_lab_workload import enforce_budget


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.sample={'windows_free_gib':2,'d_free_gib':50}
        self.ops={'ledger':{'pending':0,'started':0,'failed':0},'oldest_pending_seconds':0,'dispatcher':{'state':'ready'}}

    def test_exact_headroom_boundary_is_accepted(self): enforce_budget(self.sample,self.ops)
    def test_memory_exhaustion_stops_production(self):
        with self.assertRaises(RuntimeError): enforce_budget({**self.sample,'windows_free_gib':1.9},self.ops)
    def test_disk_exhaustion_stops_production(self):
        with self.assertRaises(RuntimeError): enforce_budget({**self.sample,'d_free_gib':49},self.ops)
    def test_backlog_stops_production(self):
        with self.assertRaises(RuntimeError): enforce_budget(self.sample,{**self.ops,'ledger':{'pending':101,'started':0,'failed':0}})
    def test_old_work_stops_production(self):
        with self.assertRaises(RuntimeError): enforce_budget(self.sample,{**self.ops,'oldest_pending_seconds':91})
    def test_failed_or_recovering_consumer_stops_production(self):
        with self.assertRaises(RuntimeError): enforce_budget(self.sample,{**self.ops,'dispatcher':{'state':'recovering'}})
        with self.assertRaises(RuntimeError): enforce_budget(self.sample,{**self.ops,'ledger':{'pending':0,'started':0,'failed':1}})


if __name__=='__main__': unittest.main()
