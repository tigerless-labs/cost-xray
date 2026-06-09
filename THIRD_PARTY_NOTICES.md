# Third-Party Notices

cost-xray is licensed under the MIT License (see [LICENSE](LICENSE)). It incorporates or adapts
work from the projects below, whose licenses are reproduced/linked here as required.

## Adapted source

**llm-interceptor** — https://github.com/chouzz/llm-interceptor — MIT License, © Chouzz.
The SSE-parsing and request/response redaction approach was adapted from this project. Its MIT
license requires that its copyright and permission notice be retained; see the upstream
[LICENSE](https://github.com/chouzz/llm-interceptor/blob/master/LICENSE) for the authoritative
text.

## Runtime dependencies

These are installed from PyPI and ship under their own licenses (not vendored here); listed for
transparency:

- **mitmproxy** — https://github.com/mitmproxy/mitmproxy — MIT License. The local capture engine,
  including the local CA used for locked-endpoint agents.
- **tiktoken** — https://github.com/openai/tiktoken — MIT License.
- **rich** / **textual** — https://github.com/Textualize — MIT License.
- **litellm** (optional, pricing) — https://github.com/BerriAI/litellm — MIT License.
