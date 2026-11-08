# mixpool

**Broadcast Bitcoin transactions and query mempool APIs through the Nym mixnet.**

By apeir (https://apeir.dk/online/).

`mixpool` is a small command-line tool and Python library. It sends a signed raw
Bitcoin transaction to public Esplora APIs (mempool.space, blockstream.info,
mempool.emzy.de, plus testnet/signet ones) **through the [Nym](https://nym.com)
mixnet**, so the API operator and anyone watching your network never see the
broadcast come from your IP address. Fee estimates, block height and transaction
status lookups go through the mixnet too.

It has no Python dependencies (stdlib only). Since 0.2.0 the mixnet is reached
through [nymdial](https://github.com/apeirs/nymdial), a small Rust binary on
Nym's own `nym-sdk` and `nym-smolmix`: each connection mixpool makes is a fresh,
short-lived mixnet client inside a `nymdial` process. There is no
`nym-socks5-client` to install, init and keep running, and no local proxy port.
The old `nym-socks5-client` path is still there behind `--transport socks5`.

## Why

When you broadcast a transaction to a public API (or through a wallet that does
it for you), the server learns *"this IP address published transaction X first"*.
That one line of a log links your network identity to your coins: the inputs
you spent, your change address, and from there often your whole wallet. The same
holds for the first time you look up a fresh address or txid. Block explorers,
the companies that run them, hosting providers and anyone on the network path
can keep that link.

Running your own node fixes part of this, but most people don't. Tor helps, but
Tor exit traffic is low-latency and open to timing correlation by an observer who
sees both ends. Nym is a mixnet: packets are fixed-size Sphinx packets, they are
delayed and reordered at every hop, and clients send cover traffic. That makes
it much harder to match "packet left this user" with "request arrived at the API".

mixpool makes that the easy path for one specific job: getting a transaction
from you to the Bitcoin network without the network knowing it was you.

## How the mixnet is used

```
 mixpool ══ socketpair ══> nymdial connect HOST 443 ──Sphinx packets──> entry gateway ──> 3 mix layers ──> exit gateway
 (TLS + HTTP here)          (child process: a fresh                                                     IP Packet Router
                             mixnet client + userspace TCP/IP)                                                │ TCP (TLS inside)
                                                                                                              ▼
                                                                                    mempool.space / blockstream.info / ...
```

Default transport, `nymdial`:

1. **A fresh mixnet client per connection.** For every request mixpool starts
   `nymdial connect HOST PORT --events` with one end of a socketpair as its
   stdin and stdout. nymdial creates a new ephemeral Nym client (new keys, new
   identity), picks an exit from the nym-api (IP Packet Router exits,
   performance-weighted random order), health-checks the tunnel and moves on to
   the next exit if one doesn't answer. It reports the exit it used (node id,
   country, IPs) as a JSON line on stderr, and mixpool shows it.
2. **A fresh identity per broadcast.** `broadcast` posts to every endpoint in
   parallel, each over its own new client, so no mixnet identity is shared
   between endpoints, between broadcasts, or with your lookups.
3. **Optional random delay.** `broadcast --delay 30-300` makes each endpoint wait
   its own random time (drawn from the OS random source) before its client is
   started and the transaction posted, so the moment the transaction appears is
   not the moment you pressed enter.
4. **TLS is end to end** between mixpool and the API, as before: the socket mixpool
   gets back is wrapped with Python's `ssl` and the certificate is checked against
   the real hostname. The exit relays ciphertext.
5. **DNS goes through the mixnet.** The hostname is passed to nymdial unresolved;
   nymdial resolves it with DNS-over-HTTPS through its tunnel (Quad9, Mullvad,
   Cloudflare at pinned IPs). Your local resolver never sees it.
6. **No clearnet fallback, ever.** If nymdial is missing, mixpool exits with an
   error (code 3); if no exit works, the request fails. mixpool never opens a
   direct connection.
7. The transaction is parsed locally first: malformed hex fails before any
   packet is sent, and the txid is computed locally and compared with what the
   API answers (a 2xx with a different txid is not counted as success).

The cost: every connection pays a tunnel setup, typically 5 to 15 s, more when
nymdial has to skip exits that don't answer. See the live numbers below.

### The socks5 transport (opt-in)

`--transport socks5` (or `MIXPOOL_TRANSPORT=socks5`) uses a running
`nym-socks5-client` instead, as mixpool 0.1 did:

1. `mixpool setup` asks the Nym API for exit gateways that are in the active set,
   run a network requester and apply the Nym exit policy, picks one **at random**
   (performance >= 0.95), and initialises a `nym-socks5-client` that uses it.
   By default the client uses **reply SURBs**, so the exit does not even learn
   your mixnet address.
2. Every request opens a fresh SOCKS5 stream. The Nym client chops it into
   Sphinx packets and sends them through the entry gateway and three mix layers
   to the exit gateway, whose network requester opens the TCP connection to the
   API. Replies come back the same way.
3. **TLS is end to end** between mixpool and the API. The exit sees only the
   destination host:port and ciphertext; certificates are checked against the
   real hostname.
4. **DNS never touches your local resolver.** By default hostnames are resolved
   with DNS-over-HTTPS *over the mixnet* (Cloudflare 1.1.1.1, then Google
   8.8.8.8, both reached by IP literal) and the stream goes to an IPv4 address.
   This is not just tidiness: during testing, one exit gateway's IPv6 route to
   mempool.space was broken, so letting the exit resolve the dual-stack name made
   every mempool.space request stall, while the same request to its IPv4 address
   through the same exit worked every time. `--dns exit` hands the hostname to
   the exit instead.
5. **No clearnet fallback, ever.** If the SOCKS5 port is closed mixpool exits
   with an error (code 3) instead of making a direct connection.
6. `broadcast` posts the transaction to **every** endpoint for the network, in
   parallel, each over its own mixnet stream (but all through the one
   long-running client and its one exit).

## Install

Requires Python 3.11+ on Linux or macOS (the nymdial transport hands the child
process a Unix socketpair), and the `nymdial` binary (Rust 1.85+ to build it):

```bash
git clone https://github.com/apeirs/mixpool && cd mixpool
pip install --user .            # or just run: python3 -m mixpool ...

cargo install --git https://github.com/apeirs/nymdial    # puts nymdial in ~/.cargo/bin
mixpool ip                      # which IP does a web server see? should be the exit nymdial used
```

mixpool finds nymdial on `PATH`, or pass `--nymdial PATH` / set `MIXPOOL_NYMDIAL`.

### Only for `--transport socks5`

Nym publishes `nym-socks5-client` binaries for Linux x86_64; on other platforms build it from the [nym repo](https://github.com/nymtech/nym) or point mixpool at any running Nym SOCKS5 proxy.

Get the Nym SOCKS5 client from the official releases
(`nym-binaries-v2026.19-fuqing` was used for everything below):

```bash
mkdir -p ~/nym && cd ~/nym
curl -LO https://github.com/nymtech/nym/releases/download/nym-binaries-v2026.19-fuqing/nym-socks5-client
curl -LO https://github.com/nymtech/nym/releases/download/nym-binaries-v2026.19-fuqing/hashes.json
sha256sum nym-socks5-client   # compare with "nym-socks5-client" in hashes.json
chmod +x nym-socks5-client
export MIXPOOL_NYM_SOCKS5=~/nym/nym-socks5-client
```

Then start a mixnet client on a random active exit gateway:

```bash
mixpool setup --transport socks5     # init + run nym-socks5-client in the background on 127.0.0.1:1080
mixpool ip --transport socks5        # which IP do web servers see? should be the exit gateway's
```

Already running your own `nym-socks5-client` (or NymVPN's SOCKS5 proxy)? Skip
`setup` and pass `--transport socks5 --proxy HOST:PORT` (or set
`MIXPOOL_TRANSPORT=socks5` and `MIXPOOL_PROXY`).

> **Note on nym-socks5-client 1.1.86 (v2026.19):** `init` writes a config file
> that `run` then refuses to load (`missing field credential_requests_database`).
> `mixpool setup`/`start` patch the config automatically by adding the default
> path. If you run the client by hand, add
> `credential_requests_database = '<client dir>/data/credential_requests.db'`
> under `[storage_paths]`.

## Usage

```bash
mixpool broadcast <hex | file | ->           # send a signed tx to every mainnet endpoint
mixpool broadcast tx.hex --network signet    # mainnet | testnet4 | testnet | signet
mixpool broadcast tx.hex --dry-run           # parse + print txid only, send nothing
mixpool broadcast tx.hex --delay 30-300      # each endpoint waits a random 30-300 s first
mixpool status <txid>                        # confirmed? which block?
mixpool fees                                 # recommended sat/vB
mixpool tip                                  # block height
mixpool decode tx.hex                        # offline: txid, wtxid, vsize, outputs
mixpool ip                                   # the IP a server sees, checked against the exit nymdial used
mixpool exits                                # the exits nymdial picks from (socks5: network requesters)
mixpool nym-status | start | stop            # socks5 only: manage the background nym-socks5-client
```

Useful flags: `-v` (trace the mixnet path, the exit and timings on stderr),
`--json`, `--endpoint URL` (repeatable, use your own Esplora), `--timeout`,
`--retries` (each retry is a new nymdial process, so a new exit),
`--country CC` and `--exit NODE` (passed to nymdial), `--setup-timeout`
(default 180 s per connection, exit failover included). socks5 only:
`--proxy`, `--dns doh|exit`, `--id`, `--binary`.

Exit codes: `0` ok / at least one endpoint accepted the tx, `1` error,
`2` no endpoint accepted the tx, `3` no mixnet transport (nymdial missing, or
the SOCKS5 port is closed), `4` (`ip`) the server saw an IP that isn't the
exit's.

### As a library

```python
from mixpool import MixnetClient, broadcast, fees, NETWORKS

c = MixnetClient()                       # nymdial transport (nymdial on PATH)
info, results = broadcast(c, NETWORKS["mainnet"], raw_hex, delay=(30, 300))
print(info.txid, [(r.endpoint, r.accepted, r.exit, r.message) for r in results])
print(fees(c, NETWORKS["mainnet"]))

c5 = MixnetClient(proxy="127.0.0.1:1080")  # socks5 transport
```

`MixnetClient.get/post` work for any HTTPS API, not just Esplora. With nymdial,
`Response.exit` says which exit the request left from. `mixpool.nymdial.Nymdial`
gives you the raw socket if you need a protocol other than HTTP.

## Live run with nymdial (Nym mainnet, 2026-11-08)

Ten runs, each one `ip`, `tip`, `fees`, `status` of a block-700,000 transaction,
and `broadcast` of that already-confirmed transaction to all three mainnet
endpoints with `--delay 1-5`. Every connection was a fresh nymdial process.
Nothing was spent: Bitcoin Core rejects the transaction because its inputs are
already spent, and getting that error back proves the POST crossed the mixnet,
reached `sendrawtransaction`, and the answer came back.

```
$ mixpool broadcast examples/already-confirmed-tx.hex --delay 1-5
each endpoint gets a fresh mixnet client (new identity, own exit), used for this POST only
REJECTED  https://mempool.space/api     HTTP 400  42.24s  via node 2300 (US)  sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}
REJECTED  https://blockstream.info/api  HTTP 400  14.97s  via node 3177 (SE)  sendrawtransaction RPC error -25: bad-txns-inputs-missingorspent
REJECTED  https://mempool.emzy.de/api   HTTP 400  18.63s  via node 3402 (CA)  sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}
```

| step (10 runs) | ok | wall time: median (min, max) |
|---|---|---|
| `ip` (the IP the server saw is the exit nymdial reported) | 10/10 | 12.8 s (9.6, 310.8) |
| `tip` | 10/10 | 60.2 s (11.1, 208.5) |
| `fees` | 10/10 | 51.8 s (10.6, 567.7) |
| `status` | 10/10 | 16.9 s (10.7, 110.4) |
| `broadcast`: at least one node verdict back | 10/10 | 95.3 s (19.0, 295.6) |
| per endpoint: mempool.space / blockstream.info / mempool.emzy.de | 10/10, 9/10, 9/10 | |

109 nymdial processes (fresh mixnet clients) across 65 distinct exits in 24
countries; 51 exit attempts failed their handshake or health check and nymdial
moved on. The two endpoint misses were a TLS handshake timeout and a read
timeout after 3 attempts each. The slow tails are exits that answered the
handshake but not the health check (about 26 s each before nymdial moves on),
plus mixpool's own retries. Typical good path: 5 to 10 s tunnel setup, 1 to 2 s
DNS and TCP through the tunnel, then the request.

## Earlier run with nym-socks5-client (live Nym mainnet, 2026-10-10)

Full logs: [`examples/live-run-2026-10-10.txt`](examples/live-run-2026-10-10.txt)
and the matching [`nym-socks5-client` excerpt](examples/nym-socks5-client-excerpt.txt).

**What the server sees.** The exit picked by `setup` was node 2682
(`ro.pierrot-nym.net`, Romania, identity `DXiKYrsx…`). ipify sees that node's IP,
not ours:

```
$ mixpool ip -v
[   0.00s] nym  our mixnet address: 5E5bRaH5…@CjaGNmprsfeRCqVSCQnq8HEUvtAJ2Pgur8UsvZZevP1d
[   0.00s] nym  entry gateway: CjaGNmprsfeRCqVSCQnq8HEUvtAJ2Pgur8UsvZZevP1d
[   0.00s] nym  exit (network requester): AfpyBUmh…@DXiKYrsx4bnGimcxC3VKpZM8VpayRnE9fsKJZAhgWEYp
[   0.00s] nym  anonymous replies (SURBs): True
[   1.79s] dns  api.ipify.org -> 172.67.74.152, 104.26.12.205, 104.26.13.205 (DoH via 1.1.1.1, over mixnet, 1.78s)
[   1.79s] send GET https://api.ipify.org -> socks5 127.0.0.1:1080 -> mixnet -> 104.26.13.205:443 (attempt 1)
[   3.61s] recv HTTP 200 OK from api.ipify.org in 1.82s (14 bytes)
https://api.ipify.org sees this request coming from 193.142.58.124 (a Nym exit gateway)
```

`193.142.58.124` is the IP that node 2682 (`DXiKYrsx…`) announces in the Nym
API, i.e. the exit gateway named in the provider address above.

**GET over the mixnet: fee estimates and tx status.**

```
$ mixpool fees -v
[   1.95s] dns  mempool.space -> 103.165.192.208, 103.165.192.202, ... (DoH via 1.1.1.1, over mixnet, 1.93s)
[   1.95s] send GET https://mempool.space/api/v1/fees/recommended -> socks5 127.0.0.1:1080 -> mixnet -> 103.165.192.206:443 (attempt 1)
[   3.48s] recv HTTP 200 OK from mempool.space in 1.53s (74 bytes)
fastestFee   2 sat/vB
halfHourFee  1 sat/vB
hourFee      1 sat/vB
economyFee   1 sat/vB
minimumFee   1 sat/vB
(https://mempool.space/api, via Nym mixnet)

$ mixpool status b2b8328e5e7af3f17449109b449517cc6641b7a1a525e0c1d019cda078ee03be -v
[   1.57s] send GET https://mempool.space/api/tx/b2b8…03be/status -> socks5 127.0.0.1:1080 -> mixnet -> 103.165.192.206:443 (attempt 1)
[   3.69s] recv HTTP 200 OK from mempool.space in 2.11s (144 bytes)
confirmed in block 700000 (0000000000000000000590fc0f3eba193a278534220b2b37e9849e1a770ca959)    (https://mempool.space/api, via Nym mixnet)
```

**POST of a raw transaction over the mixnet.** No coins were spent: the test
transaction is a real, already-confirmed one from block 700,000
([`examples/already-confirmed-tx.hex`](examples/already-confirmed-tx.hex)). Each
API passes it to its Bitcoin Core node, which rejects it because its inputs are
already spent. Getting that Bitcoin Core error back proves the full round trip:
our POST crossed the mixnet, reached `sendrawtransaction`, and the answer came
back through the mixnet.

```
$ mixpool broadcast examples/already-confirmed-tx.hex -v
txid b2b8328e5e7af3f17449109b449517cc6641b7a1a525e0c1d019cda078ee03be  (216 vB, 1 in / 2 out)
[   1.77s] send POST https://mempool.emzy.de/api/tx -> socks5 127.0.0.1:1080 -> mixnet -> 178.63.87.163:443 (attempt 1)
[   1.89s] send POST https://blockstream.info/api/tx -> socks5 127.0.0.1:1080 -> mixnet -> 23.155.96.128:443 (attempt 1)
[   2.02s] send POST https://mempool.space/api/tx -> socks5 127.0.0.1:1080 -> mixnet -> 103.165.192.207:443 (attempt 1)
[   3.99s] recv HTTP 400 Bad Request from mempool.space in 1.97s (85 bytes)
[   4.02s] recv HTTP 400 Bad Request from mempool.emzy.de in 2.25s (85 bytes)
[   4.96s] recv HTTP 400 Bad Request from blockstream.info in 3.07s (64 bytes)
REJECTED  https://mempool.space/api  HTTP 400  1.97s  sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}
REJECTED  https://blockstream.info/api  HTTP 400  3.07s  sendrawtransaction RPC error -25: bad-txns-inputs-missingorspent
REJECTED  https://mempool.emzy.de/api  HTTP 400  2.25s  sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}
```

The same on signet, and the "no clearnet fallback" check:

```
$ mixpool broadcast examples/already-confirmed-tx.hex --network signet
REJECTED  https://mempool.space/signet/api  HTTP 400  3.40s  sendrawtransaction RPC error: {"code":-25,"message":"bad-txns-inputs-missingorspent"}
REJECTED  https://blockstream.info/signet/api  HTTP 400  3.54s  sendrawtransaction RPC error -25: bad-txns-inputs-missingorspent

$ mixpool stop && mixpool fees
error: no SOCKS5 proxy at 127.0.0.1:1080 ([Errno 111] Connection refused). Is nym-socks5-client running? Refusing to fall back to a direct (clearnet) connection.
```

Round trips took about 1.5 to 3.5 s per request with reply SURBs on (plus
about 2 s for the first DoH lookup of each host, then cached for 5 minutes).

## Threat model and limits (read this)

What mixpool protects:

- **The API operator** (mempool.space, Blockstream, ...) sees the request come from a
  Nym exit gateway's IP, not yours.
- **Your ISP / local network** sees encrypted Sphinx traffic to a Nym entry
  gateway, mixed with cover traffic. It does not see which API you talk to, or that
  you broadcast a transaction at all.
- **The entry gateway** knows your IP but not where your packets go.
- **The exit gateway** sees the destination IP and port, the TLS server name (SNI)
  and an encrypted TLS stream. On the nymdial path the IP Packet Router answers
  through reply SURBs by construction, so it never learns your mixnet address
  either; on socks5, reply SURBs are on by default.
- **Other requests of yours.** With nymdial each connection is a new mixnet
  client and usually a different exit, so the API can't link your broadcast to
  your lookups by exit IP or mixnet identity (it still sees what you ask for).
- **Your DNS resolver** never sees the API hostnames.

What it does **not** protect against:

- **The transaction itself.** Its inputs, outputs, amounts, script types, fee rate and
  change pattern are public forever. A mixnet hides *who sent it*, not *what it says*.
  Address reuse and common-input heuristics still link your coins on chain.
- **Being the first node to see your transaction.** The API's Bitcoin node relays it
  to the P2P network. Your own node, if you run one and it also relays the tx, can
  give you away; broadcast from one place only.
- **Correlating lookups with the broadcast.** If you look up your own addresses on
  the same API from your normal IP, the API can link them. Do all lookups through
  mixpool too.
- **Timing on a quiet path.** Nym adds per-hop delays and cover traffic, but a global
  adversary watching your connection and the API at the same moment, with little
  other traffic, has a better chance at correlation than against a busy network.
- **A malicious exit** can drop or delay your request (TLS stops it from changing it).
  That's why mixpool posts to several APIs and checks the returned txid.
- **Fingerprints.** mixpool sends a generic `User-Agent`, but its TLS handshake is
  Python's, which is not unique to you yet is not a browser's either.
- **Setup traffic.** nymdial (and, on socks5, `mixpool setup` / `exits` and the
  Nym client) fetch the exit list and network topology from the public Nym API
  directly, not over the mixnet. That reveals that you use Nym and when you
  start a client, nothing about any transaction.
- **Timing of the setup.** A fresh client per connection means a burst of
  setup traffic right before each request. `--delay` spreads broadcasts out; it
  doesn't hide that you made one.
- **Free bandwidth today.** At the time of writing gateways let mixnet SDK/SOCKS5
  clients run in "disabled credentials mode" (no zk-nym ticket needed). If that
  changes, you will need Nym bandwidth credentials; mixpool doesn't handle that yet.

This is young software by a single author. Read the code (about 1,400 lines of stdlib Python, plus
nymdial) before trusting it with something that matters.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v          # offline: fake nymdial, fake SOCKS5 proxy, fake Esplora
MIXPOOL_LIVE=1 python3 -m unittest tests.test_live -v   # live: through the real mixnet (nymdial on PATH)
```

The offline suite (42 tests, 4 of them live and skipped by default) runs a fake
`nymdial` binary and checks that every request is its own process with the
hostname passed unresolved, that each broadcast and each endpoint gets a fresh
process, that `--delay` is drawn per endpoint and waited before posting, that the
exit options reach nymdial, that a missing nymdial never leads to a direct
connection, that setup errors retry with a new process and name the failed step,
that a hung setup is timed out and its process stopped, and that the CLI refuses
`--proxy` without `--transport socks5`. It also checks the SOCKS5 encoding (hostnames go to the proxy, nothing is
resolved locally), that a closed proxy never leads to a direct connection, endpoint
failover, fee-format fallback, accept/reject handling, that a 2xx with the wrong txid
isn't counted, and txid/wtxid/vsize against real mainnet transactions. The live suite
fetches fees and a tx status and checks that Bitcoin Core's rejection of an
already-confirmed tx comes back through the mixnet.

## Prior art

- [sgeisler/btcbc-rs](https://github.com/sgeisler/btcbc-rs) (2021) and
  [elsirion/broadnym](https://github.com/elsirion/broadnym) (2025) broadcast
  transactions over Nym to a **dedicated server** (a Nym service provider someone has
  to run) that then calls an API. mixpool needs no extra server: it goes through
  Nym's public exit gateways straight to existing public APIs, sends to several of
  them, and also covers the lookups (fees, status, height) that leak just as much.

## Future plans

- Electrum-protocol servers (TCP/TLS) and Bitcoin Core `sendrawtransaction` over
  Nym's IP packet router, so it also works with your own node or Electrum servers.
- Liquid, and Lightning-adjacent lookups (e.g. LNURL/BOLT12 offers fetching).
- Wallet integration (a drop-in "broadcast via Nym" for Sparrow/Electrum users).
- Bandwidth credential (zk-nym) support for when free bandwidth ends.

## License

MIT. See [LICENSE](LICENSE).
