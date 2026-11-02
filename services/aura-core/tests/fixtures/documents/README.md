# Document retrieval evaluation fixtures

The labelled corpus used by AURA-0070 is intentionally represented by stable
fixture identifiers and bounded expectations.  `runner.py` materializes
bounded strict-parser PDF/DOCX bytes and Markdown/plain-text samples in its
own test process, then sends every format through upload, processing, and
retrieval.  Evaluator output contains only counts, ranks, locators, and
outcome metadata.  No fixture content, filename, query, owner, or provider
payload is allowed in retained evaluation artifacts.

`manifest.yaml` is the replay manifest.  It is safe to retain and review as a
test asset because its labels are opaque IDs rather than document text.
