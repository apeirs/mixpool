# mixpool

**Broadcast Bitcoin transactions and query mempool APIs through the Nym mixnet.**

By apeir (https://apeir.dk/online/).

`mixpool` is a small command-line tool and Python library. It sends a signed raw
Bitcoin transaction to public Esplora APIs (mempool.space, blockstream.info,
mempool.emzy.de, plus testnet/signet ones) **through the [Nym](https://nym.com)
mixnet**, so the API operator and anyone watching your network never see the
broadcast come from your IP address. Fee estimates, block height and transaction
status lookups go through the mixnet too.

It has no Python dependencies (stdlib only). The mixnet itself is handled by
Nym's official `nym-socks5-client` release binary, which mixpool can set up and
run for you.

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
 mixpool ──SOCKS5──> nym-socks5-client ──Sphinx packets──> entry gateway ──> 3 mix layers ──> exit gateway
 (your machine)       (your machine)       + cover traffic                                    network requester
                                                                                                    │ TCP (TLS inside)
                                                                                                    ▼
                                                                              mempool.space / blockstream.info / ...
```

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
6. The transaction is parsed locally first: malformed hex fails before any
   packet is sent, and the txid is computed locally and compared with what the
   API answers (a 2xx with a different txid is not counted as success).
7. `broadcast` posts the transaction to **every** endpoint for the network, in
   parallel, each over its own mixnet stream, so one API being down or filtering
   an exit does not stop propagation.

## Install

Requires Python 3.11+. Nym publishes `nym-socks5-client` binaries for Linux x86_64; on other platforms build it from the [nym repo](https://github.com/nymtech/nym) or point mixpool at any running Nym SOCKS5 proxy.

```bash
git clone https://github.com/apeirs/mixpool && cd mixpool
pip install --user .            # or just run: python3 -m mixpool ...
```

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
mixpool setup        # init + run nym-socks5-client in the background on 127.0.0.1:1080
mixpool ip           # which IP do web servers see? should be the exit gateway's
```

Already running your own `nym-socks5-client` (or NymVPN's SOCKS5 proxy)? Skip
`setup` and pass `--proxy HOST:PORT` (or set `MIXPOOL_PROXY`).

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
mixpool status <txid>                        # confirmed? which block?
mixpool fees                                 # recommended sat/vB
mixpool tip                                  # block height
mixpool decode tx.hex                        # offline: txid, wtxid, vsize, outputs
mixpool exits                                # active exit gateways with a network requester
mixpool nym-status | start | stop            # manage the background mixnet client
```

Useful flags: `-v` (trace the mixnet path and timings on stderr), `--json`,
`--endpoint URL` (repeatable, use your own Esplora), `--timeout`, `--retries`,
`--dns doh|exit`.

Exit codes: `0` ok / at least one endpoint accepted the tx, `1` error,
`2` no endpoint accepted the tx, `3` mixnet client not reachable.

### As a library

```python
from mixpool import MixnetClient, broadcast, fees, NETWORKS

c = MixnetClient(proxy="127.0.0.1:1080")
info, results = broadcast(c, NETWORKS["mainnet"], raw_hex)
print(info.txid, [(r.endpoint, r.accepted, r.message) for r in results])
print(fees(c, NETWORKS["mainnet"]))
```

`MixnetClient.get/post` work for any HTTPS API, not just Esplora.

## Real run (live Nym mainnet, 2026-10-10)

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
- **The exit gateway** sees the destination host and an encrypted TLS stream. With
  reply SURBs (the default) it does not learn your mixnet address either.
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
- **Setup traffic.** `mixpool setup` / `exits` and the Nym client itself fetch the
  network topology from the public Nym API directly (not over the mixnet). That
  reveals that you use Nym, nothing about any transaction.
- **Free bandwidth today.** At the time of writing gateways let mixnet SDK/SOCKS5
  clients run in "disabled credentials mode" (no zk-nym ticket needed). If that
  changes, you will need Nym bandwidth credentials; mixpool doesn't handle that yet.

This is young software by a single author. Read the code (about 1,000 lines of stdlib Python) before
trusting it with something that matters.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v          # offline: fake SOCKS5 proxy + fake Esplora
MIXPOOL_LIVE=1 python3 -m unittest tests.test_live -v   # live: through the real mixnet (needs mixpool setup)
```

The offline suite checks the SOCKS5 encoding (hostnames go to the proxy, nothing is
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

- Separate mixnet identities per broadcast (one throwaway client per tx) and an
  option to wait a random delay before posting.
- Electrum-protocol servers (TCP/TLS) and Bitcoin Core `sendrawtransaction` over
  Nym's IP packet router, so it also works with your own node or Electrum servers.
- Liquid, and Lightning-adjacent lookups (e.g. LNURL/BOLT12 offers fetching).
- A native build on the Rust `nym-sdk` so no separate client binary is needed, and
  wallet integration (a drop-in "broadcast via Nym" for Sparrow/Electrum users via
  the SOCKS5 port).
- Bandwidth credential (zk-nym) support for when free bandwidth ends.

## License

MIT. See [LICENSE](LICENSE).
