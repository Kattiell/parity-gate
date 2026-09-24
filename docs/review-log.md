# Review log

The tool has been through two adversarial reviews. The first was given one
instruction above all others: **make it report PASS on a change that would
break a real consumer.** It found two ways. The second asked whether everything
the repository says about itself is actually executed somewhere, and whether
the gate's detection can be measured rather than argued.

This page is the record of what came out: the finding, what changed, and what
now fails if it comes back. It is deliberately short on method.

## 2026-09-12

| Severity | Finding | Fix | Pinned by |
| --- | --- | --- | --- |
| Blocker | Differential mode reported PASS when the status code changed: a check existed only where somebody had written `expect_status`, which is the work the tool exists to make unnecessary | the observed status sets of both sides are compared on every case | `test_a_status_divergence_fails_without_anyone_having_predicted_it` |
| Blocker | A collection that came back empty read as five breaking `FIELD_REMOVED` findings, so any endpoint with a filter or pagination failed the build on a healthy service | an empty collection makes its item subtree *unobserved*, reported separately as "not checked": not a finding, and not silence either | four tests, including one that keeps the fix from becoming a blanket amnesty under arrays |
| Major | `max_retries` defaulted to `1`, retrying a flaky endpoint into a `200` before flakiness triage ever saw it | the default is `0` | the demo no longer has to override it |
| Major | Redaction leaked several common credential shapes and destroyed common QA data (barcodes, phone numbers, unpunctuated CNPJs) | card masking is gated behind a Luhn checksum, more prefixed shapes added, and the boundary is stated plainly in SECURITY.md | `test_redaction.py` |
| Major | `pip install .` produced a package that could not run its own demo | demo suites ship in the wheel, resolved with `importlib.resources` | a test that runs the demo from outside a clone |
| Major | Strictly sequential, a fresh socket per call: 1200 calls took eight minutes at 400 ms RTT | `policy.workers` plus a per-host connection pool on `http.client`. It also surfaced credentials being replayed across a host change, now stripped | sixteen tests, including a redirect outside the allow-list refused before the packet |
| Major | Identity matching fell back to positional comparison in silence, so the noise got blamed on the API | emits `KEY_MATCH_UNAVAILABLE` naming the key and what happened instead | `test_differ.py` |
| Minor | `preflight` did not resolve credentials, so a missing variable surfaced mid-run | resolved at the door | |
| Minor | Contract mode printed "0 value diff", which reads as "no values changed" rather than "values were not checked" | it says the latter | |
| Minor | The secret scanner flagged English prose and its own test fixtures | the matched value now has to look like a credential, and deliberate fixtures carry an explicit pragma so the exemption is visible in the file | `test_english_prose_about_tokens_is_not_a_finding` |

Cloning it fresh and following the README as a stranger produced three more:
a credential was reported as redacted on requests that had sent none (masking
`null` protects nothing and hides whether the header went out at all), there
was no way to run a subset of a large suite (`--filter`), and evidence grew
without bound (`--keep N`).

The same pass closed two gaps that turned out to be decisions rather than
limits: **GraphQL** (a protocol problem only in what it takes away, the
operation type and the error signal) and **OpenAPI** (`import-openapi`, which
removes the largest piece of adoption friction). gRPC and long-lived streams
stay out of scope, with the README naming who to use instead.

## 2026-09-13

| Severity | Finding | Fix |
| --- | --- | --- |
| Blocker | The README carried a CI badge and SECURITY.md described a scan "on every build". No workflow file existed and GitHub showed zero runs. The previous review had recorded the claim as fixed | `.github/workflows/ci.yml` exists and runs. If a claim is not a step in that file, the README no longer makes it |
| Major | `timeout_seconds` was a socket timeout, which every arriving byte resets. A server trickling one byte at a time held a run open indefinitely | a deadline for the whole body, with the socket timeout shrunk to what is left of it before each read. Pinned by a test that trickles at one byte per 50 ms against a one-second deadline |
| Major | A response body had no size limit | `policy.max_response_bytes`, 10 MiB. A declared oversize is refused before reading, an undeclared one is cut off |
| Major | A body nested 5000 levels deep raised `RecursionError`, which escaped the worker and took the whole run down: no report, every finished case discarded | nesting beyond 64 levels is refused at decode time, and any unexpected exception while judging a case becomes an `ERROR` record for that case while the others complete |
| Major | One item of four whose `price` came back as a string made the field's types a union, classified as a risky widening, and the case passed with a warning. That is exactly how the defect ships | a new type outside the numeric family is breaking. Found by the selftest on its first run, which had reported it as 19 missed faults in differential mode and 21 in contract mode. After the fix: 187 of 187 and 137 of 137 detected, no false alarms |

Added in the same pass: `parity-gate selftest` (fault injection measured against
the real judging functions), stable rule ids with SARIF output, `[[waivers]]`
with an enforced expiry, and `parity-gate history` for flakiness read across
runs rather than across three back-to-back calls.

## Still open

Stated here rather than papered over:

- **Headers can still be trickled.** The status line and headers are read under
  the per-read timeout and `http.client`'s own limits, not under the body
  deadline.
- **The evidence chain is not a signature.** Whoever can edit the records can
  recompute it. Signing the manifest in CI would close that for bundles
  produced there; it is not done.
- **Enumerations are not part of the contract.** A string field whose set of
  values grows is invisible to a shape-only contract, and a strict consumer can
  break on it. Recording value sets would need an opt-in that respects ADR-002.
- **Latency is a median of a few samples**, the first of which pays for the
  connection, and differential mode does not compare the two sides' latency.
- **The selftest's 100% is over a fixed fault model**, and `history` only sees
  as far back as the bundles kept.
- **Redaction is pattern-based.** A high-entropy value under an innocuous key
  is not caught.
