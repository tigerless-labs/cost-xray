# Security Policy

cost-xray sits in the path of your coding agent's API traffic to decode it. That places it in a
trust-sensitive position, so its security properties are explicit guarantees, not implementation
details.

## What cost-xray does and does not do

- It runs a local mitmproxy hop that captures **your own** agent's requests and responses to the
  model API, on your own machine. It is not a network service and accepts no remote input.
- It sends **no telemetry**. Nothing leaves your machine.
- Captured prompts, code, and derived analysis stay under `~/.cost-xray/`. Delete that directory
  to clear all captured data.

## Guarantees

- **Local-only binding.** The proxy binds to `127.0.0.1`; it is never reachable off-host.
- **Credentials are redacted before disk.** `authorization`, `x-api-key`, cookies, and
  secret-looking body fields are stripped in the proxy hooks *before* anything is written. The
  read layer reads credentials it needs from your own agent config, never from the capture. This
  is enforced by tests that feed secrets through the real hooks and assert they land redacted.
- **No tokenization or analysis in the hot path.** The proxy only records redacted bytes;
  all derivation happens later in a separate process, so a bug in analysis cannot affect or
  observe live traffic beyond what was already stored.

## The local CA (Codex and other locked-endpoint agents)

Claude Code is captured via a reverse-proxy base-URL override and needs **no certificate**.

Agents with a hard-locked HTTPS endpoint (e.g. Codex) require a local mitmproxy CA so the proxy
can read their TLS. That CA:

- is **scoped to the wrapped command only**, injected via that command's environment;
- is **never added to your operating-system trust store** — selecting a Codex/all install is
  your explicit consent to generate it;
- lives under your user directory and can be removed by uninstalling.

If you do not capture a locked-endpoint agent, no CA is created.

## Supported versions

cost-xray is pre-1.0 and installs from a checkout; security fixes land on `master`. Run the
latest `master`.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public GitHub issue.

- Email **ryan@tigerless.com** with a description, affected version/commit, and reproduction
  steps.
- You'll get an acknowledgement, and we'll coordinate a fix and disclosure timeline with you.

Responsible disclosure is appreciated and credited.
