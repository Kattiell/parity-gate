# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/Kattiell/parity-gate/security/advisories/new)
rather than a public issue. This is a personal project, not a product: expect a
reply within a few days, not an SLA.

## Threat model

`parity-gate` is a test tool that is handed credentials and pointed at internal
services, and its output gets attached to tickets and uploaded as CI artifacts.
Three things follow from that.

### It must not leak what it is given

- Credentials live in environment variables. A suite stores the variable *name*
  (`auth = "env:NAME"`), never a value, and a suite containing something shaped
  like a live token is refused before it runs.
- Everything written to disk passes through `redaction.py` first: sensitive
  headers by name, sensitive JSON keys at any depth, and credential shapes with
  a recognisable **prefix** (JWT, `Bearer …`, `ghp_…`, `xox…`, `AKIA…`, `sk-…`,
  `rk_live_…`, `glpat-…`, `npm_…`, `dop_v1_…`, `AIza…`, PEM blocks,
  `user:pass@host` in URLs) plus personal data (e-mail, CPF, CNPJ, and card
  numbers that pass a Luhn check).
- **What that does not cover, stated plainly:** a credential with no prefix
  (an AWS *secret* access key, a bare 32-character API key, an opaque session id)
  appearing as a naked value under an innocuous key. Nothing here will catch
  it. Put credentials behind one of the names in `SENSITIVE_KEYS`, or review
  evidence before publishing it.
- Card masking is gated behind Luhn on purpose. Masking every 13-to-19-digit
  run destroyed barcodes, phone numbers and timestamps in the evidence while
  letting longer numbers through, and a rule that mangles real data produces
  evidence nobody can use.
- Response bodies are capped in the evidence file so a large payload cannot
  balloon an artifact.
- The integration test asserts that a token supplied through the environment
  does not appear anywhere in the serialised run.

### It must not become the incident

- **Host allow-list.** `policy.allowed_hosts` is mandatory and has no implicit
  allow-all. A host not on the list is refused.
- **Production guard.** A hostname containing `prod`, `prd`, `producao` or
  `production` is refused unless `--allow-production` is passed explicitly.
- **Writes are doubly gated.** A non-idempotent method runs only when the case
  declares `mutating = true` *and* the run passes `--allow-mutations`.
- **Private networks are opt-in.** Loopback and RFC1918 targets require
  `allow_private_networks = true`, so a suite cannot be aimed at internal
  infrastructure by accident.
- **Redirects are re-validated.** Every hop is checked against the same
  allow-list *before* the next request is sent, so a staging host that 302s to
  production does not get followed.
- **Credentials are dropped when a redirect changes host.** `Authorization`,
  `Proxy-Authorization` and `Cookie` are stripped before following a hop to a
  different scheme, host or port. A token issued for one host must not be
  replayed to another, even one the allow-list permits.
- **Connections are pooled per host and owned by one thread.** A `Client` is
  not thread-safe; the runner keeps one per worker, so a credential set for one
  target cannot ride a socket shared with another.
- **Fail before sending.** Every URL, method and credential is resolved up
  front; a misconfigured run makes zero requests.
- **A misbehaving service cannot hang or exhaust the run.** `timeout_seconds`
  is a deadline for the whole response body, not a per-read timeout that every
  trickled byte would reset; bodies above `max_response_bytes` (10 MiB by
  default) are refused before they are held in memory; and JSON nested more
  than 64 levels deep is refused at decode time rather than recursed into.
  Status line and headers are still read by `http.client` under the per-read
  timeout and its own header limits, so a server that trickles *headers* can
  stretch an exchange.
- **A defect in the tool fails one case, not the run.** An unexpected exception
  while judging a case becomes an `ERROR` record for that case; the rest of
  the run and its evidence are still written.
- **Retries are off by default.** Beyond hiding flakiness, a retry loop against
  a struggling service is extra load at the worst moment.
- **One worker by default.** Concurrency is opt-in, because a QA tool that
  quietly puts eight times the load on someone's staging environment is a bad
  guest.

### Its output must be trustworthy

Records are hash-chained: each record's hash covers its content and the hash of
the record before it, and the chain head is written to a manifest alongside the
SHA-256 of every file. `parity-gate verify` recomputes both.

This detects accidental alteration: a truncated upload, a partially synced
artifact, a report edited by hand before being pasted into a ticket. It is not
proof against a determined author, who can recompute the chain. It is not
claimed to be.

## Supply chain

The runtime has **zero third-party dependencies**; it is standard library only.
`ruff`, `mypy` and `pytest` are development dependencies and are not imported by the
tool. CI runs with `permissions: contents: read` and no secrets, and the tool
scans its own repository for credential-shaped strings on every build.

## Known limits

- Redaction is pattern-based and prefix-anchored. See the explicit
  non-coverage above; review evidence before attaching it to a public issue.
- The production guard matches on hostname substrings. An environment whose
  production host does not say "prod" is not covered by it; the host allow-list
  is the control that is.
- Hash chaining detects alteration, not forgery.
