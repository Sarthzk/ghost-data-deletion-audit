# Token Usage and Cost Report

Final full-dataset run that produced `output.csv`.

## How the system uses models

- **Providers:** openai. The pipeline supports OpenAI (`openai` SDK) and Anthropic (`anthropic` SDK); it uses whichever key is configured.
- **Where an LLM is used:** only for (1) reading screenshots and PDFs into structured records (`media`), and (2) deciding whether a multilingual request text authorises deletion (`intent`).
- **Everything else is deterministic Python:** identity resolution, evidence linking, state reconstruction, policy application, step ordering. It costs 0 tokens.
- **Token efficiency:** each media file is sent once. Request texts are deduplicated (550 requests incl. samples, far fewer unique texts) and classified 40 per call. All responses are cached on disk, so re-running `python3 code/main.py` makes **0** calls.

## Per-model totals

| Model | Calls | Input tok | Output tok | Cache write | Cache read | Total tok | Cost (USD) |
|---|---:|---:|---:|---:|---:|---:|---:|
| gpt-5-mini-2025-08-07 | 25 | 69,556 | 29,878 | 0 | 0 | 99,434 | $0.0771 |
| **Overall** | 25 | 69,556 | 29,878 | 0 | 0 | 99,434 | $0.0771 |

## By purpose

| Purpose | Calls | Input tok | Output tok | Cache write | Cache read | Total tok | Cost (USD) |
|---|---:|---:|---:|---:|---:|---:|---:|
| intent | 10 | 22,673 | 15,062 | 0 | 0 | 37,735 | $0.0358 |
| media | 15 | 46,883 | 14,816 | 0 | 0 | 61,699 | $0.0414 |
| **Overall** | 25 | 69,556 | 29,878 | 0 | 0 | 99,434 | $0.0771 |

## Per-request averages

- Requests in `deletion_requests.csv`: **518**
- Total tokens: **99,434**, average **192.0 tokens per request**
- Estimated total cost: **$0.0771**, average **$0.00015 per request**
- Cost covers all 550 requests (518 + 32 samples) and all 15 media files; the per-request figure divides by 518.

Prices per million tokens (input / output / cached input): gpt-5-mini $0.25 / $2.0 / $0.025.

## Notes

- When no API credentials are present, the pipeline still runs end to end. It uses the rule-based intent classifier and skips media, and makes 0 calls.
- Cached re-runs of the final pipeline: 0 model calls, 0 tokens, $0. A run **without any API key** reads the same cached answers and reproduces `output.csv` byte for byte.
- Excluded above: 2 extra call(s) made while scoring our custom evaluation cases (1,127 tokens, $0.0006).
