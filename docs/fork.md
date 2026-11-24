# What this fork changes

`metalmon/otari` is a fork of [`mozilla-ai/otari`](https://github.com/mozilla-ai/otari)
kept for one deployment shape: a **closed perimeter** — a network with no route to the
public internet, where every service the gateway talks to runs on the same machines.

Everything here exists because that shape needs it. Nothing is a matter of taste, and
nothing is carried for its own sake: a change that upstream adopts should leave this
fork the moment it lands there.

## The four changes to the gateway

### A fetch provider that runs a page's scripts, without a hosted service

`any-fetch` ships four providers. `builtin` needs no service but does not execute
JavaScript, so a page that draws itself returns an empty document; `exa`, `tabstack`
and `deepseek` do render, and are hosted APIs with no address a closed deployment can
reach. The intersection — self-hosted *and* renders — was empty.

`any-fetch/src/any_fetch/providers/firecrawl.py` fills it. It is written against
[Firecrawl Simple](https://github.com/metalmon/firecrawl-simple), the community fork
that keeps the scrape API and drops the hosted extras, but the request and response it
uses are the ones upstream Firecrawl answers too, so a deployment may run either.

Two details are worth keeping in mind when touching it, and both are explained at the
code:

- Firecrawl validates its request body **strictly** — an unknown key is a 400, not an
  ignored field — so native options are declared one by one rather than passed through
  as a bag.
- `metadata.sourceURL` is the URL as **asked**, assigned before the fetch, so it is not
  the address after redirects. The provider reports `reports_final_url=False` for that
  reason; reading redirects out of it would be wrong.

### `direct_available` in `GET /api/v1/tools`

A managed tool has two ways in: the gateway offers it to a model inside a chat
completion, and a client calls `POST /api/v1/search` or `/fetch` directly, which goes
through `DirectToolService` and a different set of conditions. Discovery reported only
the first.

A client that drives the direct endpoints cannot read that answer. Volt, the client
this deployment serves, treats a tool whose direct path it cannot confirm as absent, so
web search never appeared — with no error anywhere, because nothing had failed.

`src/gateway/api/routes/tools.py` now reports `direct_available` beside `available`.
This is the one change here that is not about the perimeter at all: the gap is in the
contract, and it is the first candidate to offer upstream.

### A key may travel in the clear to a private address

`_validate_transport` refused any `api_base` that was not `https` when the entry
carried an `api_key`. In a closed deployment the gateway and its search or fetch
backend sit on one container network with no certificate to present each other, which
made every keyed provider unusable — the same home-lab case `provider_allow_private_hosts`
already declines to block elsewhere.

`src/gateway/core/settings/tools.py` now permits plain http when the host cannot be
public: a loopback or private IP literal, a bare name with no dot (a service name on a
container network), or one of the suffixes reserved for private use. A public endpoint
still has to be https, where the key would cross networks the deployment does not own.
The test is deliberately conservative and runs at configuration load, so it judges by
name alone: a container name resolves only once the network is up, and a deployment
must not need DNS to start.

### Asking Firecrawl for less time than the client will wait

Firecrawl gives up at its own `timeout` and answers. The adapter would rather have that
answer than a dead connection, so it asks for slightly less than it will wait. Asking
for more is the bug this guards: the client cuts the connection first, the page is lost
either way, and all that is left to report is a transport failure.

## Compose

### The `web-fetch` profile

Upstream's compose starts the bundled backends behind profiles — `code-exec`,
`web-search`, `object-storage`. There was no profile for fetching, because none of the
rendering providers has anything to run.

`--profile web-fetch` starts four containers in the same shape: the Firecrawl API, its
queue, its worker, and the browser that runs the scripts. The gateway reaches them over
the Docker network, so `config.yml` names `http://firecrawl:3002` and nothing depends
on the host:

```yaml
fetch_tools:
  pages:
    provider: firecrawl
    api_base: "http://firecrawl:3002"
    api_key: "00000000-0000-0000-0000-000000000000"   # any UUID; it keeps no key store
    timeout: 120
    price_per_call: 0
web_fetch_default_tool: pages
```

### Built entrypoints, not development loops

The Firecrawl services are started with `node dist/...` rather than their package
scripts. `start:production` runs `tsc` before starting — recompiling on every container
start, and requiring the TypeScript compiler in the runtime image — and `workers` runs
through nodemon and ts-node.

### The gateway's own image

`image:` defaults to `docker.io/metalmon/otari:latest`, this fork's build. Pulling the
fork and running compose otherwise started upstream's gateway, without either of the
changes above. `$OTARI_IMAGE` still overrides it, which is how a locally built image is
tried without editing the file.

## Published images

| Image | What it is |
| --- | --- |
| `metalmon/otari` | this fork's gateway |
| `metalmon/firecrawl-api` | the Firecrawl API and its worker |
| `metalmon/firecrawl-browser` | the browser that runs a page's scripts |

Their sources live in [`metalmon/firecrawl-simple`](https://github.com/metalmon/firecrawl-simple).

## Carrying this forward

Upstream is the source of truth; this fork rebases onto it rather than merging, so the
list above stays readable as a list of commits. Before a rebase, re-read this file and
ask of each entry whether upstream has since covered it — `direct_available` is the one
most likely to go that way, and the fork is better off the day it does.
