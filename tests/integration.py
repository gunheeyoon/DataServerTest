import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

exe = str(pathlib.Path(sys.argv[1]).resolve())


def connect(port):
    deadline = time.monotonic() + 5
    while True:
        try:
            s = socket.create_connection(('127.0.0.1', port), timeout=2)
            f = s.makefile('rwb', buffering=0)
            assert f.readline() == b'HELLO 3\n'
            return s, f
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(.05)


def request(f, text):
    f.write((text + '\n').encode())
    return f.readline().decode().strip()


def check_udp_streams(f, f2):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second:
        for receiver in (first, second):
            receiver.bind(('127.0.0.1', 0))
            receiver.settimeout(2)
        first_port = first.getsockname()[1]
        second_port = second.getsockname()[1]

        assert request(f, 'SUB 65536 x') == 'ERR invalid_port'
        assert request(f, f'SUB {first_port} missing') == 'ERR unknown_subscription'
        assert request(f, f'SUB {first_port} calendar') == 'ERR stream_denied'
        assert request(f, f'SUB {first_port} x yaw') == 'ERR expected_sub_port_and_name'
        assert request(f, f'SUB {first_port} telemetry') == 'OK SUB'
        assert first.recv(4096) == b'UPDATE telemetry 1 speed=12.5 mode=active\n'
        assert request(f, 'UNSUB telemetry') == 'OK UNSUB'
        # Drain any telemetry datagrams queued before the unsubscribe reply.
        first.settimeout(.05)
        while True:
            try:
                assert first.recv(4096).startswith(b'UPDATE telemetry ')
            except socket.timeout:
                break
        first.settimeout(2)
        assert request(f, f'SUB {first_port} position') == 'OK SUB'
        assert request(f, f'SUB {first_port} x') == 'OK SUB'
        # roll is streamable even though GET is denied by the test configuration.
        assert request(f2, f'SUB {second_port} attitude') == 'OK SUB'
        assert second.recv(4096) == b'UPDATE attitude 1 roll=0 pitch=0 yaw=0\n'

        assert request(f, 'PING') == 'OK PONG'
        started = time.monotonic()
        counts = {'position': 0, 'x': 0}
        expected = {'position': 'x=0 y=0 z=0', 'x': 'x=0'}
        for _ in range(240):
            kind, name, sequence, fields = first.recv(4096).decode().strip().split(' ', 3)
            counts[name] += 1
            assert kind == 'UPDATE' and int(sequence) == counts[name]
            assert fields == expected[name]
            if min(counts.values()) >= 60:
                break
        else:
            raise AssertionError('Both subscriptions must deliver 60 updates')
        elapsed = time.monotonic() - started
        assert .7 < elapsed < 2, elapsed

        assert request(f, 'UNSUB position') == 'OK UNSUB'
        assert request(f, 'UNSUB position') == 'ERR subscription_not_active'
        # Move x to a new port and verify its sequence survives the change.
        assert request(f, f'SUB {second_port} x') == 'OK SUB'
        for _ in range(240):
            kind, name, sequence, fields = second.recv(4096).decode().strip().split(' ', 3)
            if name == 'x':
                assert kind == 'UPDATE' and int(sequence) > 60 and fields == 'x=0'
                break
            assert name == 'attitude' and fields == 'roll=0 pitch=0 yaw=0'
        else:
            raise AssertionError('Rerouted subscription did not arrive')

        first.settimeout(.15)
        for _ in range(120):
            try:
                first.recv(4096)
            except socket.timeout:
                break
        else:
            raise AssertionError('UDP stream continued on the old port')
        assert request(f, 'SUB false') == 'OK SUB'
        assert request(f, 'PING') == 'OK PONG'
        while not second.recv(4096).startswith(b'UPDATE attitude '):
            pass


def check_publication(port, owner, reader):
    def wait_for(control, command, expected, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = request(control, command)
            if reply == expected:
                return
            time.sleep(.01)
        raise AssertionError(f'{command}: expected {expected}, got {reply}')

    def start(control, group):
        reply = request(control, f'PUBLISH {group}').split()
        assert reply[:4] == ['OK', 'PUBLISH', group, str(port)]
        assert len(reply) == 5 and len(bytes.fromhex(reply[4])) == 16
        return reply[4]

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        def sample(token, sequence, fields):
            udp.sendto(f'SAMPLE {token} {sequence} {fields}'.encode(), ('127.0.0.1', port))

        assert request(owner, 'PUBLISH attitude') == 'ERR write_denied'
        assert request(owner, 'PUBLISH missing') == 'ERR unknown_publication'
        token = start(owner, 'input_pair')
        assert start(owner, 'input_pair') == token
        assert request(owner, 'PUBSTATUS') == 'PUBSTATUS input_pair=pending'
        assert request(reader, 'PUBSTATUS') == 'PUBSTATUS'
        assert request(reader, 'PUBLISH x') == 'ERR field_published'
        assert request(reader, 'UNPUBLISH input_pair') == 'ERR publication_not_active'
        assert request(owner, 'SET x=3') == 'ERR field_published'
        assert request(reader, 'SET weather=snow speed=3') == 'ERR field_published'
        assert request(reader, 'GET weather') == 'DATA weather=rain'

        # A disjoint group can publish concurrently, even if it isn't streamable.
        calendar = start(reader, 'calendar')
        sample(calendar, 1, 'year=2025 month=2 day=29')
        sample(calendar, 1, 'year=2028 month=2 day=29')
        wait_for(owner, 'GET calendar', 'DATA year=2028 month=2 day=29')
        sample(token, 99, 'x=99')
        sample(token, 99, 'x=99 x=99')
        sample(token, 99, 'x=99 speed=nan')
        sample(token, 1, 'speed=1 x=1')
        wait_for(reader, 'GET input_pair', 'DATA x=1 speed=1')

        # Read while another thread sends samples at 60 Hz. Neither field can
        # get ahead of the other, and duplicate/older packets cannot rewind it.
        def send_samples():
            started = time.monotonic()
            for offset in range(60):
                sequence = offset + 10
                time.sleep(max(0, started + offset / 60 - time.monotonic()))
                sample(token, sequence, f'x={sequence} speed={sequence}')
                sample(token, sequence, 'x=-1 speed=-1')
                sample(token, sequence - 1, 'x=-2 speed=-2')

        observed = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            sending = pool.submit(send_samples)
            while not sending.done():
                kind, x, speed = request(reader, 'GET input_pair').split()
                assert kind == 'DATA' and x[2:] == speed[6:]
                observed.append(int(x[2:]))
                time.sleep(.008)
            sending.result()
        assert observed == sorted(observed) and min(observed) >= 1
        assert len(set(observed)) > 1
        wait_for(reader, 'GET input_pair', 'DATA x=69 speed=69')
        # Only the initial valid sample and the 60 valid numbered samples count.
        wait_for(owner, 'PUBSTATUS', 'PUBSTATUS input_pair=61', timeout=12)
        wait_for(reader, 'PUBSTATUS', 'PUBSTATUS calendar=1')

        assert request(owner, 'UNPUBLISH input_pair') == 'OK UNPUBLISH'
        assert request(owner, 'PUBSTATUS') == 'PUBSTATUS'
        other_socket, other_control = connect(port)
        with other_socket, other_control:
            replacement = start(other_control, 'input_pair')
            assert replacement != token
            sample(token, 100, 'x=-3 speed=-3')
            sample(replacement, 1, 'x=80 speed=80')
            wait_for(reader, 'GET input_pair', 'DATA x=80 speed=80')

        # Once TCP closes, the server releases the claim and invalidates its token.
        wait_for(reader, 'SET x=90 speed=90', 'OK SET')
        sample(replacement, 100, 'x=-4 speed=-4')
        # Confirm the receive loop has processed subsequent traffic.
        sample(calendar, 2, 'year=2032 month=2 day=29')
        wait_for(owner, 'GET calendar', 'DATA year=2032 month=2 day=29')
        assert request(reader, 'GET input_pair') == 'DATA x=90 speed=90'
        assert request(reader, 'UNPUBLISH calendar') == 'OK UNPUBLISH'


with tempfile.TemporaryDirectory() as tmp:
    config = pathlib.Path(tmp) / 'test-fields.json'
    source = pathlib.Path(__file__).resolve().parents[1]
    catalog = json.loads((source / 'fields.json').read_text())
    catalog['roll'].update(readable=False, writable=False)
    catalog['speed'] = dict(type='double', default=10.25, readable=True,
                            writable=True, streamable=True)
    catalog['mode'] = dict(type='enum', values=['idle', 'active'], default='idle',
                           readable=True, writable=True, streamable=True)
    field_groups = pathlib.Path(tmp) / 'test-field-groups.json'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    args = [exe, str(port), str(config), '127.0.0.1', str(field_groups)]
    config.write_text(json.dumps(catalog))
    for invalid in ('{"bad": ["missing"]}', '{"x":["x"], "x":["y"]}', '{"bad":["x",]}'):
        field_groups.write_text(invalid)
        result = subprocess.run(args, capture_output=True, timeout=5)
        assert result.returncode == 1 and b'test-field-groups.json:' in result.stderr
    groups = json.loads((source / 'field_groups.json').read_text())
    groups.update(z=['x'], telemetry=['speed', 'mode'], input_pair=['x', 'speed'])
    field_groups.write_text(json.dumps(groups))
    for invalid in ('{"x":{"type":"double","default":true}}',
                    '{"x":{"type":"int","default":01}}',
                    '{"mode":{"type":"enum","values":["idle"],"default":"unknown"}}'):
        config.write_text(invalid)
        result = subprocess.run(args, capture_output=True, timeout=5)
        assert result.returncode == 1 and b'test-fields.json:' in result.stderr
    config.write_text(json.dumps(catalog))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        s, f = connect(port)
        s2, f2 = connect(port)
        try:
            assert request(f, 'GET x weather A_Bypass') == 'DATA x=0 weather=sunny A_Bypass=false'
            assert request(f, 'SET weather=rain A_Bypass=true') == 'OK SET'
            assert request(f2, 'GET weather A_Bypass') == 'DATA weather=rain A_Bypass=true'
            assert request(f, 'SET weather=snow roll=4') == 'ERR write_denied'
            assert request(f, 'GET weather') == 'DATA weather=rain'
            assert request(f, 'SET weather=fog') == 'ERR invalid_weather'
            assert request(f, 'SET A_Bypass=1') == 'ERR expected_true_or_false'
            assert request(f, 'SET x=nan').startswith('ERR ')
            assert request(f, 'GET missing') == 'ERR unknown_field'
            assert request(f, 'GET roll') == 'ERR read_denied'
            assert request(f, 'GET position') == 'DATA x=0 y=0 z=0'
            assert request(f, 'GET weather position') == 'DATA weather=rain x=0 y=0 z=0'
            assert request(f, 'GET z') == 'DATA z=0'
            assert request(f, 'GET weather attitude') == 'ERR read_denied'
            assert not any(part.startswith('roll=') for part in request(f, 'GET *').split())
            assert request(f, 'SET year=2024 month=2 day=29') == 'OK SET'
            assert request(f, 'SET year=2025') == 'ERR invalid_day'
            assert request(f, 'GET year') == 'DATA year=2024'
            assert request(f, 'GET calendar') == 'DATA year=2024 month=2 day=29'
            assert request(f, 'GET telemetry') == 'DATA speed=10.25 mode=idle'
            assert request(f, 'SET speed=12.5 mode=active') == 'OK SET'
            assert request(f, 'SET speed=99 mode=unknown') == 'ERR invalid_enum_value'
            assert request(f2, 'GET telemetry') == 'DATA speed=12.5 mode=active'
            assert 'x:double:SRW' in request(f, 'SCHEMA')
            assert 'mode:enum:SRW' in request(f, 'SCHEMA')
            f.write(b'PI')
            time.sleep(.02)
            f.write(b'NG\nPING\n')
            assert f.readline() == b'OK PONG\n'
            assert f.readline() == b'OK PONG\n'
            check_udp_streams(f, f2)
            check_publication(port, f, f2)
            f2.write(b'x' * 4097)
            assert f2.readline() == b'ERR line_too_long\n'
            assert f2.readline() == b''
        finally:
            f.close(); s.close(); f2.close(); s2.close()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
print('TCP commands, JSON validation, 60 Hz UDP subscriptions and exclusive UDP publications passed.')
