---
name: 2md
description: "Convert local documents to Markdown with extracted image assets, complete image transcription and visual descriptions; route structured Marker output (JSON/HTML/chunks), OCR/table-only extraction, custom processors, batch, Python, GUI, and HTTP API to Marker. Triggers: document to Markdown, PPTX/DOCX/XLSX to Markdown, scan to Markdown, extract document images and OCR, PDF to Markdown."
license: Proprietary. LICENSE.txt has complete terms
---

# Document to Markdown

> **Source repo**: https://github.com/Loveacup/2md (independent skill repository).

Choose the path by requested output; never switch engines after a failure. Read the referenced guide before running that branch.

## Route

| Request | Path |
|---|---|
| Standard Markdown from supported local document/image; export embedded images and optionally transcribe/describe them | `scripts/2md.py convert` — read [MarkItDown workflow](references/markitdown.md) |
| Default visual analysis, model selection, local OpenAI-compatible endpoint, privacy, or `--vision off` | Read [Vision workflow](references/vision.md) before conversion |
| JSON/HTML/chunks, OCR-only/table-only, complex equations, custom processors, Marker batch/Python/GUI/HTTP | Read [Marker standard](references/marker.md); for advanced native interfaces read [Marker advanced](references/marker-advanced.md) |
| Initialize isolated dependencies or check upstream version reminders | `scripts/maintenance.py init [--marker]` or `check-updates [--json]` |
| Markdown to typeset PDF or PDF editing/forms | Use sibling [2pdf skill](https://github.com/Loveacup/2pdf/blob/main/SKILL.md); 2md does not typeset or edit PDFs |

## Conversion sequence

1. Confirm the requested input files and the destination for document content and images. `auto` conversion accepts local regular files only; it does not fetch URLs, decrypt documents, or unpack archives.
2. Choose `--engine auto|markitdown|marker` based on the requested structure and format. `auto` routes image-based/scanned PDF to Marker and other supported formats to MarkItDown; routing is not a failure fallback.
3. Decide whether image vision is authorized. The default `omp @vision` sends each embedded/extracted image to the model service selected by the user's OMP configuration. For local or other explicit service use `--vision-backend openai-compatible` with the chosen endpoint and model. `--vision off` still exports images but does not transcribe Office screenshots; never describe that output as complete visual OCR.
4. Run the command from the applicable reference, inspect Markdown and every local image link, and report the `conversion.json` status and limitations. Only `status: success` together with the final `.md` is a completed conversion. On failure, report the failed stage and partial-output location; use a fresh output root for another attempt.

## Boundaries

- Batch in the unified CLI is a confirmed file-list loop of single-file conversions; Marker native batch remains available as an advanced interface.
- Embedded image transcription and description are separate fields; retain full transcription and keep alt text short and source-derived.
- Marker `--use_llm` is a separate upstream feature, disabled by default; it is not the default `omp @vision` workflow. Check Marker inference and LLM destinations before running.
- Preserve the complete output directory when handing Markdown to [2pdf](https://github.com/Loveacup/2pdf/blob/main/SKILL.md); image paths are relative to it. The two skills exchange files only and never import each other.
