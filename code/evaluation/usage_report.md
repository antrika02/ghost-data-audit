# Token usage and cost — final full-dataset run

Run: `python3 code/main.py` → `output.csv` (518 rows: 355 INCOMPLETE, 153 COMPLETE, 10 NOT_APPLICABLE),
11 s wall clock on a laptop-class CPU.

## Design choice

The audit itself (parsing, identity resolution, state reconstruction, policy, planning, ordering)
is deterministic code: it must be exact, reproducible and cheap, and every decision has to point at
an evidence id. Models are used only where the input is not machine readable:

| Stage | Model | When it is called |
| --- | --- | --- |
| Screenshot transcription (11 PNG files) | Anthropic `claude-opus-5-5` (vision) | Once per image, cached in `code/cache/image_facts.json`; re-run with `--refresh-vision` |
| Intent adjudication | Anthropic `claude-haiku-5-5` | Only for requests the multilingual rules label `AMBIGUOUS`; cached by text hash |

## Final run (the run that produced `output.csv`)

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
| --- | --- | --- | --- | --- | --- | --- |
| Anthropic | claude-opus-5-5 (vision) | 0 (11 cache hits) | 0 | 0 | 0 | $0.00 |
| Anthropic | claude-haiku-5-5 (intent) | 0 (0 ambiguous requests) | 0 | 0 | 0 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **$0.00** |

* Average tokens per request: **0** (518 requests).
* Estimated cost per request: **$0.00**.

The run prints its own counter (`ghostaudit/usage.py`): `model calls this run: 0 (deterministic pipeline; 11 cached vision extractions reused)`.

## One-time extraction cost (cache build, not part of the final run)

The screenshot cache was built once during development with Claude vision (`claude-opus-5-5`).
Token counts below are estimates (image tokens ≈ width × height / 750 after resizing to ≤ 1568 px
on the long edge), not metered values:

| Provider | Model | Calls | Input tokens (est.) | Output tokens (est.) | Total (est.) | Est. cost (USD) |
| --- | --- | --- | --- | --- | --- | --- |
| Anthropic | claude-opus-5-5 | 11 | ≈ 26,000 (≈ 2,000 image + ≈ 350 prompt each) | ≈ 4,500 | ≈ 30,500 | ≈ $0.19 |

Amortised over the 518 requests: ≈ 59 tokens and ≈ $0.0004 per request.

Prices used: claude-opus-5-5 $4 / $20 and claude-haiku-5-5 $1 / $5 per million input / output
tokens (published list prices, October 2026; constants live in `code/ghostaudit/usage.py`).

## If the optional model paths are enabled

`ANTHROPIC_API_KEY=… python3 code/main.py --refresh-vision` re-extracts the 11 screenshots
(≈ 30k tokens, ≈ $0.19 at the prices above). Intent adjudication would add one short call
(≈ 150 input / 2 output tokens, ≈ $0.0002) per ambiguous request; there were none in this dataset.
