"""Serve the browser client and relay its TCP commands and UDP snapshots."""

import argparse
import ipaddress
import json
import select
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent
SESSION_IDLE_SECONDS = 300
PACKET_WINDOW_SECONDS = 10
PUBLICATION_STATUS_INTERVAL = 1
PUBLISH_PERIOD_NS = 16_666_667
MAX_SAMPLE_PAYLOAD = 4096 - len('SAMPLE ') - 32 - 1 - 20 - 1


class PacketCounter:
    def __init__(self, now):
        self.ends_at = now + PACKET_WINDOW_SECONDS
        self.current = 0
        self.previous = None

    def advance(self, now):
        if now < self.ends_at:
            return
        windows = int((now - self.ends_at) // PACKET_WINDOW_SECONDS) + 1
        # If more than one window elapsed, the last completed one was empty.
        self.previous = self.current if windows == 1 else 0
        self.current = 0
        self.ends_at += windows * PACKET_WINDOW_SECONDS

    def record(self, now):
        self.advance(now)
        self.current += 1


class Session:
    def __init__(self, target):
        self.commands = threading.RLock()
        self.lock = threading.Lock()
        self.sender = threading.Condition(self.lock)
        self.streams = {}
        self.publications = {}
        self.acceptance_supported = True
        self.next_publication_status = 0
        self.receivers = {}
        self.closed = threading.Event()
        self.touched = time.monotonic()
        self.tcp = socket.create_connection(target, timeout=3)
        self.reader = self.tcp.makefile('rb')
        try:
            self.peer = self.tcp.getpeername()[0]
            if self.read_line() != 'HELLO 3':
                raise ValueError('Expected a server that speaks protocol v3.')
            self.default_port = self.bind_udp(0)
            self.schema = self.command('SCHEMA')
        except Exception:
            self.close()
            raise
        threading.Thread(target=self.receive_udp, daemon=True).start()
        threading.Thread(target=self.publish_udp, daemon=True).start()

    def read_line(self):
        line = self.reader.readline(1024 * 1024)
        if not line.endswith(b'\n'):
            raise ConnectionError('The data server disconnected or sent an incomplete reply.')
        return line.decode('utf-8').rstrip('\r\n')

    def bind_udp(self, port):
        with self.lock:
            if self.closed.is_set():
                raise ConnectionError('The session is closed.')
            if port in self.receivers:
                return port
            if len(self.receivers) >= 16:
                raise ValueError('This session has reached its UDP port limit.')
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Choose the local interface used by the TCP connection.
                receiver.bind((self.tcp.getsockname()[0], port))
                receiver.setblocking(False)
            except OSError:
                receiver.close()
                raise
            actual_port = receiver.getsockname()[1]
            self.receivers[actual_port] = receiver
            return actual_port

    def command(self, line):
        if not isinstance(line, str) or not line.strip() or '\n' in line or '\r' in line:
            raise ValueError('Enter one command at a time.')
        data = line.encode('utf-8')
        if len(data) > 4096:
            raise ValueError('Commands are limited to 4096 bytes.')
        parts = line.split()
        with self.commands:
            if self.closed.is_set():
                raise ConnectionError('The session is closed.')
            port = None
            if (len(parts) == 3 and parts[0] == 'SUB' and
                    parts[1].isascii() and parts[1].isdigit() and len(parts[1]) <= 4096):
                port = int(parts[1])
                if 1 <= port <= 65535:
                    try:
                        self.bind_udp(port)
                    except OSError as error:
                        raise ValueError(f'Could not listen on UDP port {port}: {error}') from error
            self.tcp.sendall(data + b'\n')
            reply = self.read_line()
            with self.lock:
                if reply == 'OK SUB':
                    if parts == ['SUB', 'false']:
                        self.streams.clear()
                    elif self.streams.get(parts[2], {}).get('port') != port:
                        self.streams[parts[2]] = {
                            'port': port, 'sequence': None, 'values': [],
                            'packets': PacketCounter(time.monotonic()),
                        }
                elif reply == 'OK UNSUB':
                    self.streams.pop(parts[1], None)
                elif reply.startswith('OK PUBLISH '):
                    _, _, name, udp_port, token = reply.split()
                    current = self.publications.get(name)
                    if current is None or current['token'] != token:
                        self.publications[name] = {
                            'port': int(udp_port), 'token': token, 'sequence': 0,
                            'accepted_last_10s': None,
                            'payload': None, 'next_send': 0, 'send_error': None,
                        }
                        self.next_publication_status = 0
                elif reply == 'OK UNPUBLISH':
                    self.publications.pop(parts[1], None)
                    self.sender.notify()
            return reply

    def refresh_publication_counts(self):
        # Status polling is faster than the counters change. Query TCP at most
        # once a second and leave the command connection free for user actions.
        if not self.commands.acquire(blocking=False):
            return
        try:
            with self.lock:
                now = time.monotonic()
                if (self.closed.is_set() or not self.publications or not self.acceptance_supported
                        or now < self.next_publication_status):
                    return
                self.next_publication_status = now + PUBLICATION_STATUS_INTERVAL
            self.tcp.sendall(b'PUBSTATUS\n')
            reply = self.read_line().split()
            with self.lock:
                if reply == ['ERR', 'invalid_command']:
                    self.acceptance_supported = False
                    return
                if not reply or reply[0] != 'PUBSTATUS':
                    raise ConnectionError('Unexpected publication status reply.')
                for item in reply[1:]:
                    name, _, count = item.partition('=')
                    if name in self.publications:
                        self.publications[name]['accepted_last_10s'] = (
                            int(count) if count.isascii() and count.isdigit() else None
                        )
        finally:
            self.commands.release()

    @staticmethod
    def sample_payload(name, values, groups):
        if not isinstance(values, str):
            raise ValueError('Choose a publication and enter its field values.')
        fields = groups.get(name)
        if fields is None:
            raise ValueError('This group is missing from the browser bridge configuration.')
        try:
            pairs = values.encode('ascii').split(b' ')
        except UnicodeEncodeError as error:
            raise ValueError('Use ASCII field names and values.') from error
        pairs = [pair for pair in pairs if pair]
        assignments = [pair.split(b'=', 1) for pair in pairs]
        if (any(len(pair) != 2 or not pair[1] or any(byte < 33 or byte > 126 for byte in pair[1])
                for pair in assignments) or
                sorted(pair[0].decode('ascii') for pair in assignments) != sorted(fields)):
            raise ValueError('Enter one name=value pair for each field in the group.')

        payload = b' '.join(pairs)
        if len(payload) > MAX_SAMPLE_PAYLOAD:
            raise ValueError('The field values are too large for a UDP sample.')
        return payload

    def active_publication(self, name):
        # Call with the session lock held.
        if self.closed.is_set() or name not in self.publications:
            raise ValueError('This publication is no longer active.')
        return self.publications[name]

    def send_datagram(self, publication, payload):
        # The session lock serializes sequence numbers and protects the socket.
        sequence = publication['sequence'] + 1
        prefix = f"SAMPLE {publication['token']} {sequence} ".encode('ascii')
        self.receivers[self.default_port].sendto(prefix + payload, (self.peer, publication['port']))
        publication['sequence'] = sequence

    def send_sample(self, name, values, groups):
        if not isinstance(name, str):
            raise ValueError('Choose a publication.')
        payload = self.sample_payload(name, values, groups)
        with self.commands, self.lock:
            self.send_datagram(self.active_publication(name), payload)

    def configure_publication(self, name, values, groups):
        if not isinstance(name, str):
            raise ValueError('Choose a publication.')
        payload = None if values is None else self.sample_payload(name, values, groups)
        with self.commands, self.sender:
            self.set_publication_payload(name, payload)

    def set_publication_payload(self, name, payload):
        # Call with the sender condition held.
        publication = self.active_publication(name)
        if payload is not None and publication['payload'] is None:
            publication['next_send'] = time.monotonic_ns()
        publication['payload'] = payload
        publication['send_error'] = None
        self.sender.notify()

    def begin_publication(self, name, values, groups):
        if not isinstance(name, str) or name not in groups or not isinstance(values, str):
            raise ValueError('Choose a writable group and enter its starting values if needed.')
        payload = self.sample_payload(name, values, groups) if values.strip() else None
        # Serialize the entire setup with commands from this browser. Read
        # defaults after reserving the group so another writer can't change them.
        with self.commands:
            with self.lock:
                already_active = name in self.publications
            reply = self.command(f'PUBLISH {name}')
            if not reply.startswith('OK PUBLISH '):
                return reply
            try:
                if payload is None:
                    with self.lock:
                        if self.active_publication(name)['payload'] is not None:
                            return reply
                    data = self.command('GET ' + ' '.join(groups[name]))
                    if data == 'ERR read_denied':
                        raise ValueError('Enter starting values for every field in this group.')
                    if not data.startswith('DATA '):
                        raise ValueError(f'Could not read starting values: {data}')
                    payload = self.sample_payload(name, data[5:], groups)
                with self.sender:
                    self.set_publication_payload(name, payload)
            except ValueError:
                if not already_active:
                    self.command(f'UNPUBLISH {name}')
                raise
            return reply

    def publish_udp(self):
        with self.sender:
            while not self.closed.is_set():
                next_deadline = None
                for publication in self.publications.values():
                    payload = publication['payload']
                    if payload is None:
                        continue
                    deadline = publication['next_send']
                    if deadline <= time.monotonic_ns():
                        try:
                            self.send_datagram(publication, payload)
                            publication['send_error'] = None
                        except OSError as error:
                            publication['send_error'] = str(error)
                        # Keep the original cadence and skip missed ticks.
                        elapsed = time.monotonic_ns() - deadline
                        deadline += (elapsed // PUBLISH_PERIOD_NS + 1) * PUBLISH_PERIOD_NS
                        publication['next_send'] = deadline
                    if next_deadline is None or deadline < next_deadline:
                        next_deadline = deadline
                delay = None if next_deadline is None else max(
                    0, (next_deadline - time.monotonic_ns()) / 1_000_000_000)
                self.sender.wait(delay)

    def receive_udp(self):
        while not self.closed.is_set():
            with self.lock:
                sockets = {receiver: port for port, receiver in self.receivers.items()}
            try:
                ready, _, _ = select.select(list(sockets), [], [], .1)
            except (OSError, ValueError):
                return
            for receiver in ready:
                try:
                    packet, address = receiver.recvfrom(65535)
                    if address[0] != self.peer:
                        continue
                    kind, name, sequence, *values = packet.decode('ascii').split()
                    if kind != 'UPDATE' or not sequence.isdigit() or len(sequence) > 20:
                        continue
                except (OSError, ValueError):
                    continue
                with self.lock:
                    stream = self.streams.get(name)
                    if stream is None or stream['port'] != sockets[receiver]:
                        continue
                    now = time.monotonic()
                    # Count arrivals separately from the newest sample shown in the browser.
                    stream['packets'].record(now)
                    previous = stream['sequence']
                    if previous is None or int(sequence) > int(previous):
                        stream.update(sequence=sequence, values=values, received=now)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            for stream in self.streams.values():
                stream['packets'].advance(now)
            return {
                'streams': [
                    dict(name=name, port=stream['port'], sequence=stream['sequence'],
                         values=stream['values'],
                         packets_last_10s=stream['packets'].previous,
                         age_ms=round((now - stream['received']) * 1000)
                         if 'received' in stream else None)
                    for name, stream in self.streams.items()
                ],
                'publications': [
                    dict(name=name, port=publication['port'], last_sent=publication['sequence'],
                         accepted_last_10s=publication['accepted_last_10s'],
                         acceptance_supported=self.acceptance_supported,
                         sending=publication['payload'] is not None,
                         sending_values=publication['payload'].decode('ascii')
                         if publication['payload'] is not None else None,
                         send_error=publication['send_error'])
                    for name, publication in self.publications.items()
                ],
            }

    def close(self):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            self.sender.notify()
            for receiver in self.receivers.values():
                receiver.close()
        try:
            self.tcp.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()
        self.tcp.close()


class Bridge(ThreadingHTTPServer):
    def __init__(self, host, port, target, groups):
        self.target = target
        self.groups = groups
        self.sessions = {}
        self.lock = threading.Lock()
        super().__init__((host, port), Handler)
        names = {host, 'localhost'} if host == '127.0.0.1' else {host}
        self.hosts = {f'{name}:{self.server_port}' for name in names}
        if self.server_port == 80:
            self.hosts.update(names)

    def server_bind(self):
        # On Windows, SO_REUSEADDR can let two bridge processes share a port.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def service_actions(self):
        with self.lock:
            expired = [key for key, session in self.sessions.items()
                       if time.monotonic() - session.touched > SESSION_IDLE_SECONDS]
            sessions = [self.sessions.pop(key) for key in expired]
        for session in sessions:
            session.close()


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, body, content_type='application/json'):
        if content_type == 'application/json':
            body = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, message):
        self.respond(code, {'error': message})

    def trusted(self):
        hosts = self.server.hosts
        origin = self.headers.get('Origin')
        return (self.headers.get('Host') in hosts and
                (origin is None or origin in {f'http://{host}' for host in hosts}))

    def session(self):
        key = self.headers.get('X-Session')
        with self.server.lock:
            session = self.server.sessions.get(key)
            if session is not None:
                session.touched = time.monotonic()
            return key, session

    def do_GET(self):
        if not self.trusted():
            return self.error(403, 'Use the configured browser console address.')
        if self.path == '/api/status':
            key, session = self.session()
            if session is None:
                return self.error(404, 'Session ended. Connect again.')
            try:
                session.refresh_publication_counts()
            except OSError as error:
                self.end_session(key, session)
                return self.error(502, str(error))
            return self.respond(200, session.snapshot())
        assets = {
            '/': ('index.html', 'text/html; charset=utf-8'),
            '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
            '/style.css': ('style.css', 'text/css; charset=utf-8'),
        }
        if self.path in assets:
            filename, content_type = assets[self.path]
            return self.respond(200, (ASSETS / filename).read_bytes(), content_type)
        self.error(404, 'Not found.')

    def do_POST(self):
        if not self.trusted() or self.headers.get_content_type() != 'application/json':
            return self.error(403, 'Use the configured browser console address.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 32768:
                raise ValueError('Invalid request size.')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object.')
        except ValueError as error:
            return self.error(400, str(error))

        if self.path == '/api/connect':
            try:
                session = Session(self.server.target)
            except (OSError, ValueError) as error:
                return self.error(502, f'Could not connect to the data server: {error}')
            key = uuid.uuid4().hex
            with self.server.lock:
                if len(self.server.sessions) < 16:
                    self.server.sessions[key] = session
                    return self.respond(200, {
                        'session': key, 'udp_port': session.default_port,
                        'schema': session.schema, 'groups': self.server.groups,
                        'target': ':'.join(map(str, self.server.target)),
                    })
            session.close()
            return self.error(503, 'Too many browser sessions.')

        key, session = self.session()
        if session is None:
            return self.error(404, 'Session ended. Connect again.')
        if self.path == '/api/disconnect':
            self.end_session(key, session)
            return self.respond(200, {})
        if self.path == '/api/command':
            try:
                reply = session.command(body.get('command'))
            except ValueError as error:
                return self.error(400, str(error))
            except OSError as error:
                self.end_session(key, session)
                return self.error(502, str(error))
            return self.respond(200, dict(reply=reply, **session.snapshot()))
        if self.path == '/api/sample':
            try:
                session.send_sample(body.get('name'), body.get('values'), self.server.groups)
            except ValueError as error:
                return self.error(400, str(error))
            except OSError as error:
                return self.error(503, f'Could not send the UDP sample: {error}')
            return self.respond(200, session.snapshot())
        if self.path == '/api/publish':
            try:
                reply = session.begin_publication(body.get('name'), body.get('values'), self.server.groups)
            except ValueError as error:
                return self.error(400, str(error))
            except OSError as error:
                self.end_session(key, session)
                return self.error(502, str(error))
            return self.respond(200, dict(reply=reply, **session.snapshot()))
        if self.path == '/api/publish-stream':
            if 'values' not in body:
                return self.error(400, 'Enter values to start or send null to stop.')
            try:
                session.configure_publication(body.get('name'), body['values'], self.server.groups)
            except ValueError as error:
                return self.error(400, str(error))
            return self.respond(200, session.snapshot())
        self.error(404, 'Not found.')

    def end_session(self, key, session):
        with self.server.lock:
            self.server.sessions.pop(key, None)
        session.close()

    def log_message(self, format, *args):
        # Status polling is frequent. Keep the local console readable.
        if self.path != '/api/status':
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    frozen = getattr(sys, 'frozen', False)
    groups_path = ROOT / 'field_groups.json'
    defaults = {}
    if frozen:
        # Deployments can override defaults beside the executable. The packaged
        # local bridge also works on its own with the bundled group catalog.
        directory = Path(sys.executable).parent
        config = directory / 'bridge.json'
        if config.is_file():
            try:
                defaults = json.loads(config.read_text(encoding='utf-8'))
                if not isinstance(defaults, dict):
                    raise ValueError('expected a JSON object')
            except (OSError, ValueError) as error:
                parser.error(f'{config}: {error}')
        groups_path = directory / 'field_groups.json'
        if not groups_path.is_file():
            groups_path = ASSETS / 'field_groups.json'

    parser.add_argument('--bind', type=ipaddress.IPv4Address, default=defaults.get('bind', '127.0.0.1'),
                        help='IPv4 address used by browsers to reach the console')
    parser.add_argument('--port', type=int, default=defaults.get('port', 8080), help='HTTP port')
    parser.add_argument('--server-host', default=defaults.get('server_host', '127.0.0.1'))
    parser.add_argument('--server-port', type=int, default=defaults.get('server_port', 9000))
    parser.add_argument('--groups', type=Path, default=groups_path)
    args = parser.parse_args()
    if args.bind.is_unspecified:
        parser.error('--bind requires a specific IPv4 address')
    groups = json.loads(args.groups.read_text(encoding='utf-8'))
    host = str(args.bind)
    try:
        bridge = Bridge(host, args.port, (args.server_host, args.server_port), groups)
    except OSError as error:
        parser.exit(1, f'Could not bind the browser bridge to {host}:{args.port}: {error}\n')
    with bridge:
        print(f'Browser console: http://{host}:{bridge.server_port}', flush=True)
        try:
            bridge.serve_forever()
        except KeyboardInterrupt:
            pass
        for session in list(bridge.sessions.values()):
            session.close()


if __name__ == '__main__':
    main()
