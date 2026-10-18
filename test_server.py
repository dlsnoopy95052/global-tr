import threading
import time
import unittest
from unittest.mock import patch
import server


class TraceTests(unittest.TestCase):
    def test_parser_retains_missing_hops_and_multipath(self):
        hops = server.parse_trace('''traceroute to example (8.8.8.8), 24 hops max
 1  10.0.0.1  1.0 ms  2.0 ms  3.0 ms  4.0 ms  5.0 ms
 2  * * * * *
 3  1.1.1.1  10.0 ms * 1.0.0.1 12.0 ms * *
 4  8.8.8.8  20.0 ms  21.0 ms  22.0 ms * *''')
        self.assertEqual([h['loss'] for h in hops], [0, 100, 60, 40])
        self.assertIsNone(hops[1]['ip'])
        self.assertIsNone(hops[2]['ip'])
        self.assertEqual(hops[2]['responders'], ['1.1.1.1', '1.0.0.1'])
        self.assertEqual(hops[3]['rtt'], 21)

    def hop(self, ttl, loss=0, destination=False):
        return dict(ttl=ttl, ip='1.1.1.1', loss=loss, replies=5-loss/20,
                    destination=destination)

    def test_each_pair_is_independent_of_downstream_loss(self):
        hops = [self.hop(1), self.hop(2,60), self.hop(3,0,True)]
        self.assertEqual(server.edge_status(hops[0],hops[1],20),'loss')
        self.assertEqual(server.edge_status(hops[1],hops[2],20),'healthy')

    def test_persistent_loss_and_healthy_paths(self):
        hops = [self.hop(1), self.hop(2,40), self.hop(3,40,True)]
        self.assertEqual(server.edge_status(hops[0],hops[1],20),'loss')
        self.assertEqual(server.edge_status(hops[1],hops[2],20),'loss')
        good = [self.hop(1), self.hop(2,0,True)]
        self.assertEqual(server.edge_status(*good,20),'healthy')

    def test_gap_or_silent_destination_remains_unknown(self):
        hops=[self.hop(1),self.hop(3,40),self.hop(4,100,True)]
        hops[2]['ip']=None
        self.assertEqual(server.edge_status(hops[0],hops[1],20),'unknown')
        self.assertEqual(server.edge_status(hops[1],hops[2],20),'unknown')

    def test_unreachable_is_not_healthy(self):
        hops=[self.hop(1),self.hop(2,0,True)]
        hops[1]['unreachable']=True
        self.assertEqual(server.edge_status(*hops,20),'unknown')

    def test_six_hops_keep_five_independent_segments(self):
        hops=[self.hop(ttl) for ttl in range(1,7)]
        hops[1]['ip']=None
        hops[1]['loss']=100
        hops[1]['replies']=0
        hops[3]['loss']=40
        hops[3]['replies']=3
        hops[-1]['destination']=True
        edges=server.build_edges(hops,20)
        self.assertEqual([(e['start'],e['end']) for e in edges],
                         [(0,1),(1,2),(2,3),(3,4),(4,5)])
        self.assertEqual([e['status'] for e in edges],
                         ['unknown','unknown','loss','healthy','healthy'])

    def test_known_unlocated_hop_preserves_healthy_adjacency(self):
        output=''' 1  1.1.1.1  1 ms  1 ms  1 ms  1 ms  1 ms
 2  10.0.0.1  2 ms  2 ms  2 ms  2 ms  2 ms
 3  8.8.8.8  3 ms  3 ms  3 ms  3 ms  3 ms'''
        location=dict(lat=1,lon=2,city='Test',country='Test')
        def lookup(ip):
            return None if ip=='10.0.0.1' else location
        with patch('server.subprocess.run') as run, patch('server.geolocate',side_effect=lookup):
            run.return_value.stdout=output
            run.return_value.stderr=''
            run.return_value.returncode=0
            route=server.collect('example.com','8.8.8.8',20)
        self.assertTrue(route['reached'])
        self.assertEqual(route['edges'],[dict(start=0,end=1,status='healthy',skipped=0),
                                                dict(start=1,end=2,status='healthy',skipped=0)])

    def test_mtr_parser_reads_loss_rtt_and_multipath(self):
        hops = server.parse_mtr('''Start: 2026-10-18T10:00:00+0000
HOST: laptop                      Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 10.0.0.1                   0.0%     5    1.0   1.2   1.0   1.5   0.2
  2.|-- ???                      100.0     5    0.0   0.0   0.0   0.0   0.0
  3.|-- 1.1.1.1                   40.0%     5   10.0  11.0  10.0  12.0   1.0
      |-- 1.0.0.1
  4.|-- 8.8.8.8                    0.0%     5   20.0  21.0  20.0  22.0   1.0''')
        self.assertEqual([h['ttl'] for h in hops], [1, 2, 3, 4])
        self.assertEqual([h['loss'] for h in hops], [0, 100, 40, 0])
        self.assertIsNone(hops[1]['ip'])
        self.assertIsNone(hops[1]['rtt'])
        self.assertIsNone(hops[2]['ip'])
        self.assertEqual(hops[2]['responders'], ['1.1.1.1', '1.0.0.1'])
        self.assertEqual(hops[3]['rtt'], 21)
        self.assertEqual(hops[3]['replies'], 5)

    def test_collect_mtr_uses_mtr_command(self):
        output=' 1.|-- 8.8.8.8   0.0%     5    1.0   1.0   1.0   1.0   0.0\n'
        location=dict(lat=1,lon=2,city='Test',country='Test')
        with patch('server.subprocess.run') as run, patch('server.geolocate',return_value=location):
            run.return_value.stdout=output
            run.return_value.stderr=''
            run.return_value.returncode=0
            route=server.collect('example.com','8.8.8.8',20,'mtr')
        self.assertEqual(run.call_args[0][0][0].split('/')[-1],'mtr')
        self.assertEqual(route['mode'],'mtr')
        self.assertTrue(route['reached'])

    def test_mtr_stream_counts_probes_replies_and_ignores_in_flight(self):
        stream = server.MtrStream()
        for line in ['x 0 100', 'h 0 10.0.0.1', 'p 0 2000 100',
                     'x 1 101', 'h 1 8.8.8.8', 'p 1 10000 101',
                     'x 0 102', 'p 0 4000 102',
                     'x 1 103', 'x 2 104']:  # 103 is in flight; ttl 3 only has an in-flight probe
            stream.feed(line, now=100.0)
        hops = stream.hops('8.8.8.8', now=100.5)
        self.assertEqual([h['ttl'] for h in hops], [1, 2])
        self.assertEqual((hops[0]['probes'], hops[0]['replies'], hops[0]['rtt']), (2, 2, 3.0))
        self.assertEqual((hops[1]['probes'], hops[1]['loss']), (1, 0.0))
        # Once the grace period passes, the unanswered probe counts as lost.
        hops = stream.hops('8.8.8.8', now=102.0)
        self.assertEqual([h['ttl'] for h in hops], [1, 2])
        self.assertEqual((hops[1]['probes'], hops[1]['loss']), (2, 50.0))

    def test_mtr_stream_drops_hops_after_destination(self):
        stream = server.MtrStream()
        for line in ['x 0 1', 'h 0 8.8.8.8', 'p 0 1000 1', 'x 1 2']:
            stream.feed(line, now=0)
        self.assertEqual([h['ttl'] for h in stream.hops('8.8.8.8', now=10)], [1])

    def test_live_mtr_stops_on_request(self):
        class FakeProc:
            stdout = ['x 0 1\n', 'h 0 8.8.8.8\n', 'p 0 1000 1\n']
            stderr = None
            def poll(self): return None if not stopped.is_set() else 0
            def terminate(self): stopped.set()
            def wait(self): pass
        stopped = threading.Event()
        location = dict(lat=1, lon=2, city='Test', country='Test')
        server.JOBS['t'] = dict(state='running', routes=[], errors=[], rev=0, stopped=False,
                                ends_at=time.time() + 300)
        server.STOPS['t'] = threading.Event()
        published = []

        def publish(route):
            published.append(route)
            server.stop_job('t')  # user presses Stop after the first update

        with patch('server.subprocess.Popen', return_value=FakeProc()) as popen, \
                patch('server.geolocate', return_value=location):
            server.collect_mtr_live('t', 'example.com', '8.8.8.8', 20, server.JOBS['t']['ends_at'], publish)
        command = popen.call_args[0][0]
        self.assertIn('--raw', command)
        self.assertNotIn('-r', command)
        self.assertGreaterEqual(len(published), 1)
        self.assertTrue(server.JOBS['t']['stopped'])
        self.assertTrue(published[0]['reached'])

    def test_live_mtr_stopped_before_first_result_is_an_error(self):
        class FakeProc:
            stdout = []
            stderr = None
            def poll(self): return 0
            def terminate(self): pass
            def wait(self): pass
        server.JOBS['u'] = dict(state='running', routes=[], errors=[], rev=0, stopped=False,
                                ends_at=time.time() + 300)
        server.STOPS['u'] = threading.Event()
        server.STOPS['u'].set()
        with patch('server.subprocess.Popen', return_value=FakeProc()):
            with self.assertRaises(RuntimeError):
                server.collect_mtr_live('u', 'example.com', '8.8.8.8', 20, server.JOBS['u']['ends_at'], print)

    def test_rejects_command_arguments_and_urls(self):
        for target in ['-I 1.1.1.1', 'https://example.com', 'example.com;id', '$(id)', '']:
            with self.assertRaises(ValueError):
                server.resolve_target(target)

    def test_private_ip_never_sent_for_geolocation(self):
        with patch('server.urllib.request.urlopen') as request:
            self.assertIsNone(server.geolocate('10.0.0.1'))
            request.assert_not_called()

    def test_unreached_destination_is_appended_as_unconfirmed(self):
        output=' 1  1.1.1.1  1 ms  1 ms  1 ms  1 ms  1 ms\n 2  * * * * *\n'
        location=dict(lat=1,lon=2,city='Test',country='Test')
        with patch('server.subprocess.run') as run, patch('server.geolocate',return_value=location):
            run.return_value.stdout=output
            run.return_value.stderr=''
            run.return_value.returncode=0
            route=server.collect('example.com','8.8.8.8',20)
        self.assertFalse(route['reached'])
        self.assertTrue(route['hops'][-1]['unobserved'])
        self.assertEqual(route['edges'][-1]['status'],'unknown')


if __name__ == '__main__':
    unittest.main()
