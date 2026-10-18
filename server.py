#!/usr/bin/env python3
"""Local, dependency-free traceroute/mtr collector and map server (macOS/Linux)."""
import concurrent.futures
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).parent
TOKEN = secrets.token_urlsafe(32)
JOBS = {}
LOCK = threading.Lock()
CACHE = {}
MISSES = {}  # ip -> time of last failed geolocation lookup
PROBES = 5
MTR_DURATION = 300  # seconds of continuous mtr measurement
STOPS = {}          # job id -> threading.Event, set when the user stops a live mtr job
PROCS = {}          # job id -> running mtr subprocesses, terminated on stop
MODES = ('traceroute', 'mtr')


def resolve_target(value):
    value = value.strip().rstrip('.')
    if not value or len(value) > 253 or not re.fullmatch(r'[A-Za-z0-9.\-]+', value):
        raise ValueError('Enter an IPv4 address or hostname, without a URL or port.')
    try:
        return value, socket.gethostbyname(value)
    except OSError:
        raise ValueError('Could not resolve destination: ' + value)


def parse_trace(output, probes=PROBES):
    hops = []
    for line in output.splitlines():
        match = re.match(r'^\s*(\d+)\s+(.*)', line)
        if not match:
            continue
        ttl, body = int(match[1]), match[2]
        ips = list(dict.fromkeys(re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', body)))
        ips = [ip for ip in ips if valid_ip(ip)]
        rtts = [float(n) for n in re.findall(r'(\d+(?:\.\d+)?)\s*ms', body)]
        # Multiple responders at one TTL cannot establish a single adjacent path.
        loss = round(100 * max(0, probes - len(rtts)) / probes, 1)
        hops.append(dict(ttl=ttl, ip=ips[0] if len(ips) == 1 else None,
                         responders=ips, loss=loss, replies=len(rtts), probes=probes,
                         rtt=round(sum(rtts) / len(rtts), 2) if rtts else None,
                         unreachable='!' in body, geo=None))
    return hops


def parse_mtr(output):
    """Parse `mtr -r -n -w` report output into the same hop shape as parse_trace."""
    hops = []
    for line in output.splitlines():
        extra = re.match(r'^\s*\|[-`]-\s+(\S+)\s*$', line)
        if extra and hops:
            # Additional responder for the previous TTL (load balancing).
            if valid_ip(extra[1]) and extra[1] not in hops[-1]['responders']:
                hops[-1]['responders'].append(extra[1])
                hops[-1]['ip'] = None
            continue
        match = re.match(r'^\s*(\d+)\.[|:][-`]-\s+(\S+)\s+([\d.]+)%?\s+(\d+)\s+([\d.]+)\s+([\d.]+)', line)
        if not match:
            continue
        ttl, host, loss, sent, _last, avg = match.groups()
        sent, loss = int(sent), float(loss)
        ip = host if valid_ip(host) else None
        replies = round(sent * (1 - loss / 100))
        hops.append(dict(ttl=int(ttl), ip=ip, responders=[ip] if ip else [],
                         loss=round(loss, 1), replies=replies, probes=sent,
                         rtt=round(float(avg), 2) if replies else None,
                         unreachable=False, geo=None))
    return hops


def valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def geolocate(ip=''):
    if ip and not ipaddress.ip_address(ip).is_global:
        return None
    if ip in CACHE:
        return CACHE[ip]
    # Live mtr republishes every second; do not re-query an address that just failed.
    if time.time() - MISSES.get(ip, 0) < 60:
        return None
    try:
        request = urllib.request.Request('https://ipwho.is/' + ip,
                                         headers={'User-Agent': 'RouteAtlas/1.0'})
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.load(response)
        if not data.get('success'):
            MISSES[ip] = time.time()
            return None
        connection = data.get('connection') or {}
        result = dict(ip=data['ip'], lat=data['latitude'], lon=data['longitude'],
                      city=data.get('city', ''), country=data.get('country', ''),
                      org=connection.get('org') or connection.get('isp') or '',
                      asn=connection.get('asn'))
        CACHE[ip] = result
        return result
    except (OSError, ValueError, KeyError):
        MISSES[ip] = time.time()
        return None


def edge_status(left, right, threshold):
    """Style each adjacent pair using the receiving hop's probe response."""
    adjacent = right['ttl'] == left['ttl'] + 1
    if not adjacent or not left.get('ip') or not right.get('ip'):
        return 'unknown'
    if (left.get('unreachable') or right.get('unreachable')
            or left.get('unobserved') or right.get('unobserved')
            or right.get('loss') is None):
        return 'unknown'
    return 'loss' if right['loss'] >= threshold else 'healthy'


def build_edges(hops, threshold):
    # Keep every hop, even without an IP or a geographic lookup result.
    return [dict(start=index - 1, end=index,
                 status=edge_status(hops[index - 1], hops[index], threshold),
                 skipped=max(0, hops[index]['ttl'] - hops[index - 1]['ttl'] - 1))
            for index in range(1, len(hops))]


def find_binary(tool):
    # mtr is often installed outside the default PATH (e.g. Homebrew or MacPorts sbin).
    extra = os.pathsep.join(['/usr/sbin', '/usr/local/sbin', '/opt/homebrew/sbin', '/opt/local/sbin'])
    return shutil.which(tool) or shutil.which(tool, path=extra)


def command_for(mode, ip, cycles=PROBES):
    if mode == 'mtr':
        binary = find_binary('mtr') or 'mtr'
        return [binary, '-n', '-r', '-w', '-c', str(cycles), '-m', '24', '-Z', '1', ip]
    binary = find_binary('traceroute') or '/usr/sbin/traceroute'
    return [binary, '-n', '-m', '24', '-q', str(PROBES), '-w', '1', ip]


def collect(name, ip, threshold, mode='traceroute'):
    command = command_for(mode, ip)
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=150)
    except FileNotFoundError:
        hint = ' (macOS: brew install mtr)' if mode == 'mtr' else ''
        raise RuntimeError(f'{mode} is missing. Install it with your OS package manager{hint}.')
    except PermissionError:
        raise RuntimeError(f'This environment blocks {mode}. Run python3 server.py in a local terminal.')
    except subprocess.TimeoutExpired:
        raise RuntimeError('Trace timed out after 150 seconds. Try a closer destination.')
    hops = (parse_mtr if mode == 'mtr' else parse_trace)(proc.stdout)
    if not hops:
        raise no_hops_error(mode, proc.stdout, proc.stderr)
    return finish_route(name, ip, hops, threshold, mode, proc.stdout,
                        proc.stderr.strip() if proc.returncode else '')


def no_hops_error(mode, stdout, stderr):
    message = stderr.strip()[:400]
    if mode == 'mtr' and 'root' in message.lower() + stdout.lower():
        message += ' mtr usually needs elevated privileges: run `sudo python3 server.py`.'
    return RuntimeError(message or f'{mode} returned no hops.')


def finish_route(name, ip, hops, threshold, mode, raw, warning):
    for hop in hops:
        hop['destination'] = ip in hop['responders'] and len(hop['responders']) == 1
    # Avoid sending private router IPs to the geolocation provider.
    ips = set(hop['ip'] for hop in hops if hop['ip']) | {ip}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        geo = dict(zip(ips, pool.map(geolocate, ips)))
    for hop in hops:
        hop['geo'] = geo.get(hop['ip'])
    reached = any(hop['destination'] and not hop['unreachable'] for hop in hops)
    if not reached:
        hops.append(dict(ttl=hops[-1]['ttl'] + 1, ip=ip, responders=[ip], loss=None,
                         replies=0, probes=hops[0]['probes'], rtt=None, geo=geo.get(ip),
                         destination=True, unobserved=True))
    edges = build_edges(hops, threshold)
    return dict(target=name, ip=ip, mode=mode, hops=hops, edges=edges, reached=reached,
                raw=raw, warning=warning,
                timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


class MtrStream:
    """Accumulate `mtr --raw` events (x = probe sent, h = responder, p = reply) per TTL."""
    GRACE = 1.5  # seconds a probe may still be in flight before it counts as lost

    def __init__(self):
        self.lock = threading.Lock()
        self.sent, self.replies, self.rtt_sum, self.responders, self.pending = {}, {}, {}, {}, {}

    def feed(self, line, now=None):
        parts = line.split()
        now = time.time() if now is None else now
        try:
            kind, ttl = parts[0], int(parts[1]) + 1  # mtr counts TTLs from zero
            with self.lock:
                if kind == 'x':
                    self.sent[ttl] = self.sent.get(ttl, 0) + 1
                    self.pending[int(parts[2])] = (ttl, now)
                elif kind == 'h' and valid_ip(parts[2]):
                    found = self.responders.setdefault(ttl, [])
                    if parts[2] not in found:
                        found.append(parts[2])
                elif kind == 'p':
                    self.pending.pop(int(parts[3]), None)
                    self.replies[ttl] = self.replies.get(ttl, 0) + 1
                    self.rtt_sum[ttl] = self.rtt_sum.get(ttl, 0.0) + int(parts[2]) / 1000
        except (IndexError, ValueError):
            pass

    def hops(self, destination, now=None):
        now = time.time() if now is None else now
        with self.lock:
            inflight = {}
            for seq, (ttl, at) in list(self.pending.items()):
                if now - at < self.GRACE:
                    inflight[ttl] = inflight.get(ttl, 0) + 1
                elif now - at > 30:
                    del self.pending[seq]
            hops = []
            for ttl in sorted(self.sent):
                probes = self.sent[ttl] - inflight.get(ttl, 0)
                if probes <= 0:
                    continue
                replies, responders = self.replies.get(ttl, 0), list(self.responders.get(ttl, []))
                hops.append(dict(ttl=ttl, ip=responders[0] if len(responders) == 1 else None,
                                 responders=responders, loss=round(100 * max(0, probes - replies) / probes, 1),
                                 replies=replies, probes=probes,
                                 rtt=round(self.rtt_sum.get(ttl, 0.0) / replies, 2) if replies else None,
                                 unreachable=False, geo=None))
        # Probes beyond the destination only time out; drop them once it has answered.
        for index, hop in enumerate(hops):
            if destination in hop['responders']:
                return hops[:index + 1]
        return hops


def mtr_summary(hops):
    lines = ['TTL  HOST                 LOSS%   SENT   AVG ms']
    for hop in hops:
        lines.append(f"{hop['ttl']:>3}  {hop['ip'] or ', '.join(hop['responders']) or '???':<20} "
                     f"{hop['loss']:>5}  {hop['probes']:>5}  {hop['rtt'] if hop['rtt'] is not None else '—':>7}")
    return '\n'.join(lines)


def collect_mtr_live(job_id, name, ip, threshold, ends_at, publish):
    """Stream one long-running mtr and publish merged per-hop results every second."""
    stop = STOPS[job_id]
    command = command_for('mtr', ip, MTR_DURATION)
    command.insert(command.index('-r'), '--raw')
    command.remove('-r')
    command.remove('-w')
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError('mtr is missing. Install it with your OS package manager (macOS: brew install mtr).')
    except PermissionError:
        raise RuntimeError('This environment blocks mtr. Run python3 server.py in a local terminal.')
    with LOCK:
        PROCS.setdefault(job_id, set()).add(proc)
    stream, started, published = MtrStream(), time.time(), False
    reader = threading.Thread(target=lambda: [stream.feed(line) for line in proc.stdout], daemon=True)
    reader.start()

    def snapshot():
        hops = stream.hops(ip)
        if not hops:
            return False
        elapsed = int(time.time() - started)
        route = finish_route(name, ip, hops, threshold, 'mtr', mtr_summary(hops),
                             f'Live mtr: sampled for {elapsed} s, updated every second. Raw output is the current per-hop summary.')
        route['started'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started))
        publish(route)
        return True

    try:
        # Stop is checked by waiting on its event, so Stop and the deadline react within a second.
        while proc.poll() is None and not stop.wait(1.0) and time.time() < ends_at:
            published = snapshot() or published
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait()
        reader.join(timeout=2)
        with LOCK:
            PROCS.get(job_id, set()).discard(proc)
    published = snapshot() or published
    if not published:
        if stop.is_set():
            raise RuntimeError('Stopped before the first mtr results arrived.')
        raise no_hops_error('mtr', '', proc.stderr.read())


def run_mtr_live(job_id, targets, threshold):
    job = JOBS[job_id]
    slots = {}

    def publish(route):
        with LOCK:
            slots[route['target']] = route
            job['routes'] = [slots[name] for name, _ in targets if name in slots]
            job['rev'] += 1

    def worker(name, ip):
        try:
            collect_mtr_live(job_id, name, ip, threshold, job['ends_at'], publish)
        except Exception as error:
            with LOCK:
                job['errors'].append(dict(target=name, message=str(error)))

    threads = [threading.Thread(target=worker, args=target, daemon=True) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def stop_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return False
        if job['state'] == 'running' and job['ends_at']:
            job['stopped'] = True
            STOPS[job_id].set()
            for proc in list(PROCS.get(job_id, ())):
                try:
                    proc.terminate()
                except OSError:
                    pass
    return True


def run_job(job_id, targets, threshold, mode='traceroute'):
    job = JOBS[job_id]
    try:
        job['source'] = geolocate()
        if mode == 'mtr':
            run_mtr_live(job_id, targets, threshold)
        else:
            for name, ip in targets:
                job['current'] = name
                try:
                    route = collect(name, ip, threshold, mode)
                    with LOCK:
                        job['routes'].append(route)
                except Exception as error:
                    with LOCK:
                        job['errors'].append(dict(target=name, message=str(error)))
    finally:
        job['state'] = 'done'
        job['current'] = None


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, payload, kind='application/json'):
        data = json.dumps(payload).encode() if kind == 'application/json' else payload
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in {f'localhost:{self.server.server_port}',
                                          f'127.0.0.1:{self.server.server_port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {'error': 'Local access only.'})
        path = self.path.split('?')[0]
        if path == '/api/session':
            return self.respond(200, {'token': TOKEN})
        if path.startswith('/api/jobs/'):
            with LOCK:
                job = JOBS.get(path.rsplit('/', 1)[-1])
                if job and job['ends_at']:
                    job = dict(job, remaining=max(0, job['ends_at'] - time.time()))
                return self.respond(200 if job else 404, job or {'error': 'Job not found.'})
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript'),
                  '/style.css': ('style.css', 'text/css')}
        if path not in assets:
            return self.respond(404, {'error': 'Not found.'})
        file, kind = assets[path]
        self.respond(200, (ROOT / 'static' / file).read_bytes(), kind)

    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Session-Token') != TOKEN:
            return self.respond(403, {'error': 'Refresh the local app and try again.'})
        stop = re.fullmatch(r'/api/jobs/([0-9a-f]+)/stop', self.path)
        if stop:
            found = stop_job(stop[1])
            return self.respond(200 if found else 404, {} if found else {'error': 'Job not found.'})
        if self.path != '/api/trace':
            return self.respond(404, {'error': 'Not found.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                raise ValueError('Request is too large or empty.')
            body = json.loads(self.rfile.read(size))
            raw = body.get('targets', [])
            if not isinstance(raw, list) or not 1 <= len(raw) <= 5 or not all(isinstance(v, str) for v in raw):
                raise ValueError('Enter between one and five destinations.')
            threshold = int(body.get('threshold', 20))
            if not 1 <= threshold <= 100:
                raise ValueError('Loss threshold must be between 1 and 100%.')
            mode = body.get('mode', 'traceroute')
            if mode not in MODES:
                raise ValueError('Mode must be traceroute or mtr.')
            targets = [resolve_target(target) for target in dict.fromkeys(raw)]
        except (ValueError, TypeError, AttributeError) as error:
            return self.respond(400, {'error': str(error)})
        with LOCK:
            if any(job['state'] == 'running' for job in JOBS.values()):
                return self.respond(409, {'error': 'A trace is already running. Wait for it to finish.'})
            job_id = secrets.token_hex(12)
            # Keep only the most recent ten jobs, in memory.
            while len(JOBS) >= 10:
                old = next(iter(JOBS))
                del JOBS[old]
                STOPS.pop(old, None)
                PROCS.pop(old, None)
            live = mode == 'mtr'
            STOPS[job_id] = threading.Event()
            JOBS[job_id] = dict(state='running', source=None, routes=[], errors=[],
                                current=None, threshold=threshold, total=len(targets), mode=mode,
                                rev=0, stopped=False, duration=MTR_DURATION if live else None,
                                ends_at=time.time() + MTR_DURATION if live else None)
        threading.Thread(target=run_job, args=(job_id, targets, threshold, mode), daemon=True).start()
        self.respond(202, {'id': job_id})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8765'))
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except PermissionError:
        raise SystemExit('Local server ports are blocked here. Run python3 server.py directly in your terminal.')
    except OSError as error:
        raise SystemExit(f'Cannot open port {port}: {error}. Try PORT=8766 python3 server.py')
    print(f'Route Atlas → http://localhost:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
