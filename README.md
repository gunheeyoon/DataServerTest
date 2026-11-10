# C++ data server

A dependency-free C++17 server for Windows and Linux. All clients share one
in-memory state. Clients use TCP to request or change values and can subscribe
to periodic snapshots over UDP. The server starts with zero position/orientation,
2026-01-01, hour/second zero, sunny weather, and false bypass flags.
Date/time are application data; they do not advance with the system clock.

## Build and run

With CMake and a C++17 compiler installed:

```powershell
cmake -S . -B build -G "MinGW Makefiles"
cmake --build build
ctest --test-dir build --output-on-failure
.\build\data_server.exe 9000
```

For Visual Studio, omit `-G "MinGW Makefiles"`, build with `--config Release`,
run tests with `-C Release`, and use `build\Release\data_server.exe`.
On Linux, omit the generator and run `./build/data_server`.
Python 3 enables the integration test; the server itself has no Python dependency.

Arguments are `data_server [port=9000] [fields_json] [bind_ipv4=127.0.0.1] [field_groups_json]`.
The default configuration files are `fields.json` and `field_groups.json` in the
working directory. Pass their paths as the second and fourth arguments to use
other files. `fields.json` replaces the previous `server.conf`.
For LAN access, provide your server's IPv4 address or `0.0.0.0` as the third
argument and permit the chosen port through the server firewall for TCP and,
if clients will publish, for UDP. Clients also need to allow incoming UDP on
their chosen receive ports. The server has no user authentication or transport
encryption.
Stop with Ctrl+C. State is reset on restart.

## Browser client

Start the C++ server, then run this in a second terminal from the project directory:

```powershell
python web_client/bridge.py
```

Open [the local console](http://127.0.0.1:8080) and select **Connect**. The
console has quick commands, live subscription cards, a one-time reply log,
and a manual command prompt. Type a name in a quick command's search box to
see eligible suggestions. The catalog on the left lists all fields, their
permissions, and the members of every group. The guide on the right describes
each command. Enter a single command, such as `GET position`,
in the prompt. For a manual subscription, use the UDP receive port shown
above the prompt, as in `SUB 50000 position` if the assigned port is 50000.

Use **New client** to open another console tab, then select **Connect** in that
tab. Each tab gets its own TCP connection, UDP receive port, subscriptions, and
reply log. All clients connected to the same C++ server share its field values.
One bridge supports up to 16 connected browser tabs.

The bridge uses Python 3's standard library. It serves HTTP on localhost,
relays commands over TCP, and receives UDP for each browser session. The browser
displays the latest snapshot from each stream without keeping a history of all
60 updates per second. Each subscription card shows how many UDP packets arrived
in its last completed 10-second window. The count includes duplicate and
out-of-order arrivals for that subscription, while the displayed values come
from its latest sequence. A tab that stops polling releases its session after
about five minutes if it has not already disconnected.

Use `--port` to change the HTTP port, `--server-host` and `--server-port` to
point to another C++ server, and `--groups` to match a custom group file used
by that server. Use `--bind` to listen on a specific IPv4 address.

### Windows executables

`build/bridge.exe` is a standalone Windows bridge with the browser assets
and Python runtime included. Its defaults are HTTP at `127.0.0.1:8080` and
the data server at `127.0.0.1:9000`.

The `LAN` folder contains a separate deployment. Keep its executables and
JSON files together on the Windows computer that owns `100.100.110.2`.
Start `data_server.exe` and `bridge.exe` from that folder. The server listens
on TCP and UDP port 9000. The bridge listens on HTTP port 8080 and connects
to the LAN server. Other computers can open
[the LAN console](http://100.100.110.2:8080). Permit inbound TCP port 8080
on the bridge host for those browsers.

`LAN/bridge.json` sets the bridge's default addresses and ports. Both LAN
executables look for their JSON configuration beside the executable.

## Field catalog

Add an entry to the top-level object in `fields.json` and restart to create
a field. The supplied `speed` field shows how to configure read, write, and
stream permissions:

```json
"speed": {
  "type": "double",
  "default": 0.0,
  "readable": true,
  "writable": true,
  "streamable": true
}
```

Clients can use `GET speed` and `SET speed=12.5`. To make a new field available
for subscriptions, add it to a group in `field_groups.json`. Each field requires a `type` and a
matching `default`. Supported types are `double` (finite JSON number),
`int` (JSON integer in the C++ `int` range), `bool` (JSON boolean), and
`enum` (string with a matching entry in the required `values` list).
Field names and enum values use 1–64 ASCII letters, digits, underscores,
or hyphens. Each permission defaults to `false` when omitted. Invalid
catalog entries prevent startup.

The supplied catalog grants these permissions:

| Fields | Stream at 60 Hz | Request reads | Client writes |
| --- | --- | --- | --- |
| x, y, z, roll, pitch, yaw | yes | yes | yes |
| speed, fuel_tank_1, fuel_tank_2 | yes | yes | yes |
| cockpit_temp, air_temp | no | yes | yes |
| year, month, day, hour, second | no | yes | yes |
| weather, A_Bypass, B_Bypass, C_Bypass | no | yes | yes |

The three flags are independent and apply to all clients. A streamable field
can be sent to subscribers even if request reads are disabled. Units and
coordinate conventions are application-defined.

## Named groups for SUB and GET

Edit `field_groups.json` on the server and restart to apply changes. Each
entry maps a group name to its fields, in the order listed. Clients can use the
same name for a UDP subscription or a one-time TCP read.
The supplied file includes:

```json
{
  "x": ["x"],
  "position": ["x", "y", "z"],
  "attitude": ["roll", "pitch", "yaw"],
  "calendar": ["year", "month", "day"]
}
```

Names are case-sensitive and must contain 1–64 ASCII letters, digits,
underscores, or hyphens. Each field list must be nonempty, with no repeated
fields. All fields must exist in the catalog. Each operation checks its own
permissions, so `GET calendar` succeeds while `SUB 50000 calendar` returns
`ERR stream_denied` with the supplied catalog.
Invalid definitions prevent startup with an error identifying the file.
An empty object `{}` makes no named streams available.

## Wire protocol v3

UTF-8/ASCII text, one line per command/response, terminated by LF (CRLF accepted).
Field names and values are case-sensitive. Maximum command length: 4096 bytes
before LF. Oversized commands produce an error and close the connection.
TCP may split or combine lines: clients must buffer until LF.

The server first sends `HELLO 3` on TCP. Streaming is initially disabled.

| Client command | Server response |
| --- | --- |
| `PING` | `OK PONG` |
| `GET x y weather` | `DATA x=0 y=0 weather=sunny` |
| `GET position` | `DATA x=0 y=0 z=0` |
| `GET *` | `DATA` followed by all readable `name=value` pairs |
| `SET weather=rain A_Bypass=true` | `OK SET` |
| `SUB 50000 position` | `OK SUB`. Starts the named stream on UDP port 50000. |
| `UNSUB position` | `OK UNSUB`. Stops only `position` on this connection. |
| `SUB false` | `OK SUB`. Stops all streams on this connection. |
| `PUBLISH speed` | `OK PUBLISH speed 9000 <token>`. Reserves the group for UDP uploads. |
| `PUBSTATUS` | `PUBSTATUS speed=600`. Counts packets accepted in each active publication's last completed 10-second window. |
| `UNPUBLISH speed` | `OK UNPUBLISH`. Releases that publication on this connection. |
| `SCHEMA` | `SCHEMA` followed by `name:type:flags` entries |

Schema flags use fixed positions: `S` for streaming, `R` for request reads, `W`
for client writes; `-` means disabled. Example: `x:double:SR-`.
The original weather enum keeps its `weather` type label in `SCHEMA`. Other
enum fields use `enum`. The supplied weather values are `sunny`, `cloudly`,
`rain`, `snow`, and `storm`. Booleans must be `true` or `false`. Doubles
must be finite. The original integer date and time fields keep their
validation, including Gregorian leap-year rules, years 1–9999, hours 0–23
and seconds 0–59.

Each SET validates a copy of the entire state and commits all supplied fields
atomically, or returns `ERR reason` without changing anything. This permits
changing a whole date in one command. Concurrent writes are serialized; the last
successful write wins. GET and UPDATE each observe a consistent snapshot.
Invalid commands, denied access, and invalid values return `ERR reason`.
Send one request at a time when matching responses; there are no request IDs.

A GET can mix field and group names, as in `GET weather position`. Group
members expand in the order listed in `field_groups.json`. A direct field
name takes precedence if a group has the same name. Every requested field must
allow reads. If a group includes a field without read permission, the entire GET
returns `ERR read_denied`.

### Subscribing over UDP

Bind an IPv4 UDP socket on the client before sending `SUB <port> <name>`
on TCP. Use the UDP socket's local port (1–65535). The server sends to the IP
address of the TCP peer, using a separate UDP source port chosen by the OS. The
specified port must be reachable at that IP, which may require UDP forwarding
when a client connects through NAT.

Each TCP connection can activate multiple named streams on the same UDP port
or on different ports. Repeating `SUB` for an active name changes its port
and keeps its sequence number. An unknown name returns `ERR unknown_subscription`.
A group with a non-streamable field returns `ERR stream_denied`. `UNSUB` on
an inactive name returns `ERR subscription_not_active`. Disconnecting TCP
ends all streams for that connection.

For `SUB 50000 position` and `SUB 50000 x`, the client receives both kinds of
datagram on UDP port 50000. Each datagram contains one complete
`UPDATE <name> <sequence> <field>=<value> ...` line with a trailing LF. The server
sends each active stream at a target rate of 60 Hz, even when values are unchanged.

```text
UPDATE position 1 x=0 y=0 z=0
UPDATE x 1 x=0
UPDATE position 2 x=0 y=0 z=0
UPDATE x 2 x=0
```

The sequence number counts send attempts separately for each active name on
each TCP connection. It starts at 1 when the stream is activated and starts
over if the stream is stopped and activated again. UDP packets can be lost,
duplicated, or reordered. TCP replies and UDP updates can arrive in either order.
Packets already in flight can still arrive after changing the port or stopping
a stream.

### Publishing over UDP

Send `PUBLISH <group>` over TCP. The group must exist in `field_groups.json`,
and every field must be writable. The reply contains the group name, UDP
destination port, and a 32-character hexadecimal token. Send to the same
server IPv4 address as the TCP connection. The UDP destination port currently
matches the TCP port. Keep the TCP connection open for the duration of the
upload. Treat the token as a credential for the publication.

For example, the supplied `speed` group can be published with this exchange:

```text
TCP request:   PUBLISH speed
TCP reply:     OK PUBLISH speed 9000 <token>
UDP datagram:  SAMPLE <token> 1 speed=12.5
UDP datagram:  SAMPLE <token> 2 speed=12.75
TCP request:   UNPUBLISH speed
TCP reply:     OK UNPUBLISH
```

Each datagram carries one complete ASCII sample, up to 4096 bytes including
any trailing newline. Include every field in the group exactly once, in any
order. Start with a positive unsigned 64-bit sequence number and increase it
for each sample. The client controls the rate. For 60 Hz, send one sample
roughly every 16.7 ms. UDP samples have no replies. Use a permitted `GET` or
`SUB` from any client to observe committed values.

The server accepts packets from the IPv4 address of the controlling TCP
connection. Unknown tokens, old or duplicate sequences, malformed packets, and
invalid field values are dropped. Only a valid sample advances the last accepted
sequence. The server commits the sample atomically, so concurrent reads see
either the prior values or the new values as a group.

Send `PUBSTATUS` over the controlling TCP connection to read the most recent
completed 10-second count for each active publication. Each window begins when
its publication starts and repeats every 10 seconds. Before the first window
closes, the reply uses `pending`, as in `PUBSTATUS speed=pending`. A completed
window with no accepted packets reports `0`. Each accepted datagram counts once,
regardless of how many fields it updates.

One active publisher owns each of its fields. Overlapping publications and
TCP `SET` commands that touch a reserved field return `ERR field_published`
without making any changes. A client can publish several disjoint groups, and
repeating `PUBLISH` for its own active group returns the same token. `UNPUBLISH`
or closing the controlling TCP connection releases its fields and invalidates
the token. In the browser console, select `speed` and click **Publish** to
reserve it and start sending at 60 Hz. Enter `speed=12.5` in Starting values
to begin with an explicit value, or leave it blank to use the current value.
A group with any unreadable fields requires explicit starting values for the
whole group. The bridge repeats the latest values on a monotonic timer.
**Apply values** changes the payload while the stream is running.
**Pause** stops sending while keeping the group reserved, and **Unpublish**
releases the group. The bridge skips missed ticks. The Sent counter records
datagrams sent by the bridge, while the acceptance counter comes from the
server. Because the windows start when you publish, wait for a full window
of continuous sending to compare the count against about 600.

Use one sender per token so the browser bridge and any separate UDP sender
do not reuse sequence numbers.

## Implementation and timing

The catalog defines the field types and permissions. The server stores
live values separately and uses a mutex for consistent reads and atomic writes.
One worker thread handles each client's TCP commands and outgoing UDP stream.
A shared UDP receive worker handles incoming samples.
Use the same mutex and validation rules when adding a simulation or hardware
producer that updates server-owned fields.

Scheduling uses a monotonic clock and an approximately 16.667 ms period. Missed
ticks are skipped instead of generating a burst. UDP sends are nonblocking and
failed sends are dropped. TCP replies time out after approximately one second
of blocked progress. There are at most 64 concurrent connections. If a future
schema makes snapshots larger than a network packet, split them into smaller
datagrams to avoid IP fragmentation. This initial design
targets modest client counts; it has no persistence, per-user authorization,
hard real-time guarantees, or graceful administrative shutdown command.
