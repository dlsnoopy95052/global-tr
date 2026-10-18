# Route Atlas

A local network route map for macOS and Linux. Runs IPv4 traceroute (or mtr) from your machine, geolocates public hop addresses, and displays multiple destinations on an interactive dark map. Includes presentation view, hop tables, raw output, JSON export, and a clearly labeled demo.

## Run

Requires Python 3.9+. No Python packages to install. Runs on macOS, Linux and Windows 10/11.

- **macOS / Linux:** needs `traceroute` (included on macOS; on Debian/Ubuntu `sudo apt install traceroute`) and, for live mode, `mtr`.
- **Windows:** nothing extra to install; it uses the built-in `tracert` and `ping`. Start with `py server.py` (or `python server.py`).

```sh
python3 server.py
```

Open **http://localhost:8765**. Enter up to five hostnames or IPv4 addresses, separated by newlines or commas. URLs and ports are not accepted. Choose **traceroute** or **mtr** under *Probe tool*. A traceroute destination can take up to 150 seconds; mtr mode runs for 5 minutes (see below). The app binds to loopback only. Set `PORT` to change the port.

### Windows live mode

Windows has no mtr, so *live monitor* finds the path once with `tracert`, then runs a continuous `ping -t` against each hop address (one probe per hop per second, same 5-minute timer and Stop button). Loss and RTT are therefore measured by pinging each router directly rather than by TTL-expiry replies, and the path is not re-discovered during the run. Routers that never answered `tracert` show as 100% loss. Windows output is parsed without relying on English text, so localized Windows should work.

### mtr mode

Runs one long mtr (`--raw` event stream, one probe per hop per second) for **5 minutes**, all destinations in parallel. Per-hop sent/loss/RTT counters update every second in the hop tables and summary; the map redraws when the path or link colours change. A countdown timer and a **Stop measurement** button appear in the sidebar; stopping early keeps the results gathered so far. Install with `sudo port install mtr` (MacPorts), `brew install mtr` (Homebrew) or `sudo apt install mtr-tiny`. Depending on how mtr was installed, it may need root to send probes (it ran fine without sudo with MacPorts mtr 0.96 on macOS); if mtr reports a permission error, start the server with `sudo python3 server.py`. mtr is also found in `/opt/local/sbin` and `/opt/homebrew/sbin`. Unlike traceroute, mtr reports per-hop loss as a percentage over its cycles, so the loss threshold behaves the same way but with the same router rate-limiting caveats.

## Map semantics

Each pair of adjacent hops gets its own line, regardless of IP geolocation availability:

- **White solid:** both adjacent hop IPs are known and the receiving hop's probe loss is below the chosen threshold (20% by default).
- **Red solid:** both adjacent hop IPs are known and the receiving hop's probe loss meets or exceeds the threshold. Later hops do not change this segment's color.
- **Dotted:** either hop IP is unknown/ambiguous, a TTL is skipped, a destination is unobserved, or an unreachable response prevents confirmation.

A six-hop trace shows six hop nodes and five independently styled connections in the hop diagram. The map keeps unlocated/private hops as labeled schematic placeholders. Initial unlocated hops stay beside the source marker; interior unlocated hops are interpolated between geographic anchors. Those positions do not imply actual router locations. Geolocation failure alone does not make a known adjacent pair unconfirmed. The public egress reference connects to the first hop with dots.

Colors describe traceroute probe responses at the receiving hop, **not proven packet loss on the physical link between the two routers**.

Five probes per TTL give coarse loss increments of 20%. Traceroute mode is a diagnostic snapshot; mtr mode samples for up to 5 minutes, then stops. Latency is source-to-hop RTT, not delay between routers. Even persistent ICMP/UDP probe loss may reflect filtering; actual application traffic can behave differently. Router rate limiting is a documented source of apparent intermediate loss: [Cloudflare’s MTR explanation](https://www.cloudflare.com/en-gb/learning/network-layer/what-is-mtr/).

Hubs stay distinct even if geolocated to the same city. The hop diagram and table retain every hop, including unknown IPs and missing coordinates. Paths use the shorter longitude span across the dateline. Traceroute reveals IP-level relationships, not physical cable routes; return paths and load balancing can affect results.

## Location and external services

The source marker uses your public egress IP, which may represent your VPN or proxy rather than your physical location. Public IPs are sent to [ipwho.is](https://ipwhois.io/documentation) using HTTPS; private addresses are never submitted. Lookup failures leave addresses geographically unlocated, with schematic placeholders on the map. Successful lookups are cached in memory for the server session. Map code loads Leaflet 1.9.4 from unpkg and key-free standard tiles from OpenStreetMap; internet access is required for the geographic map. No map API key is required. Only the tile layer is tinted dark; route colors stay unchanged. Browser requests preserve the Referer header and normal tile caching, as required by the [OpenStreetMap tile policy](https://operations.osmfoundation.org/policies/tiles/). Results remain available as a table if the map library fails to load. Trace results stay in server memory until restart; exports are explicit local downloads.

The server accepts requests only through localhost/127.0.0.1 Host headers and requires a per-session token to start traces. Targets are resolved and passed as validated IP arguments without a shell. Do not expose this development server to the internet.

## Checks

```sh
python3 -m unittest -v
```

Live traceroute requires network probe permission from the OS. If a sandbox blocks it, run the server directly in your terminal. IPv6 is not supported yet. The Windows code path has been tested only against sample `tracert`/`ping` output, not on a real Windows machine.
