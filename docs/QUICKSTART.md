# Quickstart

From a clone to a gate that fails your build, in about ten minutes.

There is no theory on this page. Every step says what to run, what you should
see, and what to do with it. Why any of it works is in [design.md](design.md).

## 0. Which mode is yours

One question decides it: what should today's API be compared against?

| Your situation | Compared against | Commands |
| --- | --- | --- |
| One API in production, no rewrite planned | its own shape, recorded earlier | `record`, then `run` |
| An API nobody has measured yet | its own repeated answers | `stability` |
| A rewrite replacing a live service | the old service | `run`, with two targets |

Most people are the first row, and that is the path below. The other two are
one flag away and marked where they differ.

You need Python 3.11 or newer and an HTTP API that returns JSON. Nothing else:
the tool has no runtime dependencies.

## 1. Install

```bash
git clone https://github.com/Kattiell/parity-gate
cd parity-gate
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

Check it before trusting it with anything of yours:

```bash
parity-gate demo --mode contract
```

You should see four cases, three of them failing, and the process should exit
`2`. That is the tool catching defects planted on purpose in a bundled mock.
Nothing leaves your machine.

If `parity-gate` is not found, `python -m parity_gate` is the same program and
takes the same arguments. See [troubleshooting](#troubleshooting).

## 2. Describe your API

The whole configuration is one TOML file. There is no code to write.

```bash
cp suites/example-api.toml suites/my-api.toml
```

Open it and change three lines, all marked in the file:

```toml
[targets.baseline]
snapshot = "contracts/my-api.json"             # 1. where the contract file goes

[targets.candidate]
base_url = "https://api.staging.example.com"   # 2. your API

[policy]
allowed_hosts = [".staging.example.com"]       # 3. what it may call, mandatory
```

Then list what you care about, one block per endpoint:

```toml
[[cases]]
id = "CAT-001"
title = "Product listing keeps its pagination envelope"
method = "GET"
path = "/products?limit=4"
```

Five to ten cases is a good first suite. Everything else has a default; the
full field reference is in [suite-reference.md](suite-reference.md).

If your API publishes an OpenAPI document, skip the typing:

```bash
parity-gate import-openapi --spec https://api.example.com/openapi.json \
                           --out suites/my-api.toml
```

**Sending a credential?** Write the variable name, never the value:
`auth = "env:MY_API_TOKEN"`. A suite containing something shaped like a live
credential is refused at load time.

## 3. Measure the noise before asserting anything

```bash
parity-gate stability --suite suites/my-api.toml
```

It calls each endpoint three times and classifies what varied:

```
  PASS  CAT-001   Product listing keeps its pagination envelope
  WARN  OPS-001   Health endpoint answers and reports uptime          - volatile_body
  FAIL  OPS-002   Inventory sync status is stable enough to compare   - flaky_status

Noise found. Add to [policy] in the suite so it stops being reported:
mask_paths = [
  "$.uptimeSeconds",
]
```

What to do with each verdict:

| Verdict | Do this |
| --- | --- |
| `STABLE` | nothing, it is trustworthy |
| `VOLATILE_BODY` | paste the printed `mask_paths` block into `[policy]` |
| `FLAKY_SHAPE`, `FLAKY_STATUS` | fix the endpoint, or drop the case. Nothing measured from an endpoint like that means anything |

Run it again until only `PASS` and masked `WARN` remain. This step is what
keeps the gate switched on six months from now.

## 4. Record the shape it has today

```bash
parity-gate record --suite suites/my-api.toml
```

It writes the contract file, one field per line. **Read it before committing.**
This is the moment you find out that a field you meant to delete last quarter
is still being served, or that something you thought was required is optional.

```bash
git add suites/my-api.toml suites/contracts/
git commit -m "Record the API contract"
```

The file holds types, requiredness and the status code each endpoint answered
with. Never values, so your catalogue can change all day without the build
noticing.

## 5. Gate on it

```bash
parity-gate run --suite suites/my-api.toml --strict
```

| Exit code | Meaning | What to do |
| --- | --- | --- |
| `0` | nothing breaking changed | ship |
| `1` | it could not run | fix the suite, the arguments or the missing credential |
| `2` | **the API changed in a way a consumer cannot survive** | read the report |
| `3` | the safety policy refused, before any request went out | see [troubleshooting](#troubleshooting) |

## 6. Put it in CI

```yaml
- name: API contract gate
  run: parity-gate run --suite suites/my-api.toml --strict --keep 20
  env:
    MY_API_TOKEN: ${{ secrets.STAGING_TOKEN }}
```

`--keep 20` keeps the last twenty evidence bundles and prunes the rest, which
is what later lets `parity-gate history evidence/` tell an intermittent failure
from a real regression.

For findings as pull-request annotations, add `--sarif parity-gate.sarif` and
upload it: the snippet is in the [README](../README.md#in-ci).

## When it fails

Open `report.md` in the evidence directory the run names. It gives you the
path, the severity and what a consumer would experience:

```
| breaking | TYPE_CHANGED  | $.products[].price | number -> string |
```

Two outcomes, and only two:

- **The change was a mistake.** The ticket is written for you.
- **The change was intended.** Re-record and commit the new contract. The diff
  is the review, and the file is one field per line precisely for that.

Working on one endpoint? `--filter 'CAT-*'` narrows the run without editing the
suite.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `parity-gate: command not found` | the script is not on `PATH`, common on Windows where pip installs it under `Scripts\` | run `python -m parity_gate` instead, same arguments |
| It runs, but behaves like an older version | an earlier install is shadowing the clone | run `python -c "import parity_gate; print(parity_gate.__file__)"`. If that path is not your clone, reinstall with `pip install -e .` inside the venv, or run `PYTHONPATH=src python -m parity_gate` |
| Exit `3`, host is not allowed | `allowed_hosts` does not cover the target | add the host. A leading dot (`.example.com`) matches subdomains |
| Exit `3`, production refused | the host name contains `prod`, `prd` or `producao` | pass `--allow-production`, out loud, or point it at staging |
| Exit `3`, private address refused | the target sits on a private network | allow it explicitly in the policy |
| Exit `1`, missing credential | the environment variable named in `auth = "env:NAME"` is not set | export it, or fix the name |
| The suite is refused as holding a credential | a live value was pasted into the TOML | move it to the environment and reference it with `env:` |
| Everything fails right after `record` | the contract was recorded against a different environment or dataset | re-record against the environment you will gate |
| The report is full of differences on a reordered list | items are being compared by position | set `[policy.array_keys]`, for example `"$.products" = "id"` |
| A case reports `KEY_MATCH_UNAVAILABLE` | the key you chose is not unique on both sides | pick one that is, or remove the entry |
| Fields come back as "not checked" | today's filter matched nothing, so the item fields were never observed | expected. Nothing was learned about them, which is not the same as their removal |

## Where to go next

- [suite-reference.md](suite-reference.md): every field of the suite file, and
  how to accept a known finding on the record with a waiver
- [design.md](design.md): how the noise is handled, how the gate measures
  itself, and how the code is laid out
- [rules.md](rules.md): what every finding id means
