# 2md — Document to Markdown

Extract document structure into Markdown with real image assets, full image transcription, and separate visual descriptions. Structured Marker output and native advanced interfaces remain available; PDF typesetting/editing/forms stay in sibling [2pdf](https://github.com/Loveacup/2pdf/blob/main/SKILL.md).

> **Source repository**: [Loveacup/2md](https://github.com/Loveacup/2md), an independently published skill. Public availability does not grant an open-source license; see [LICENSE.txt](LICENSE.txt) for the inherited terms.

## Choose a route

| Need | Use |
|---|---|
| Standard document-to-Markdown conversion | `scripts/2md.py convert` — [conversion and output contract](references/markitdown.md) |
| Configure image analysis or protect data destinations | [Vision reference](references/vision.md) |
| JSON/HTML/chunks, OCR/table-only, custom processor, GUI, HTTP, native Marker batch | `scripts/marker_runner.py` — [Marker](references/marker.md), [advanced interfaces](references/marker-advanced.md) |
| Typeset Markdown to PDF, edit PDF, fill forms | [2pdf](https://github.com/Loveacup/2pdf/blob/main/SKILL.md) |

The default vision route is OMP `@vision` and sends selected images to the service configured for that role. Choose an explicit OpenAI-compatible endpoint/model for another or local backend, or use `--vision off` to export without visual analysis. Local endpoint selection does not imply that service itself is offline. See the [privacy and model limits](references/vision.md).

## Setup and example

```bash
python3.12 scripts/maintenance.py init
python3 scripts/2md.py preflight --json
python3 scripts/maintenance.py check-updates --json
python3 scripts/2md.py convert presentation.pptx --output-dir ./out
python3 scripts/2md.py convert scan.pdf --output-dir ./out --engine marker --vision off
```

`maintenance.py init --marker` additionally initializes Marker core. Without that flag, initialization installs only the lightweight environment. It preflights each requested environment first, skips healthy environments, and runs the existing setup followed by another preflight only when needed. `check-updates --json` checks PyPI release metadata for the MarkItDown and Marker manifests, plus the installed `omp` version against official GitHub releases. It never installs updates; unavailable metadata is reported and makes the command exit nonzero. JSON uses `schema_version`, `checked_at`, `overall` (`ok`/`unavailable`), and `updates[]` entries with `name`, `source`, `current`, `pinned`, `latest`, `status` (`available`/`update_available`/`unavailable`), `update_available`, and source `url`.

The lightweight environment is controlled by `JZ2MD_VENV` (default `~/.venvs/2md`); Marker uses `JZ2MD_MARKER_VENV` (default `~/.venvs/2pdf-marker`). Both overrides are preserved by maintenance commands. Setup is isolated from the sibling 2pdf environment.

The [dependency reminder workflow](.github/workflows/dependencies.yml) runs Mondays at 08:23 UTC and can be dispatched from GitHub Actions. It updates one issue with project pins and a fresh CI baseline, not workstation versions; it never changes project dependencies. Unavailable metadata is reported explicitly and fails the workflow.

A completed conversion yields `OUT/<stem>/<stem>.md`, local image assets, and `conversion.json` with `status: success`. Keep the complete directory when using the Markdown in 2pdf because its image links are relative. An existing output directory is never overwritten; failures remain partial and must not be reported as successful delivery.

The MarkItDown environment is separate from the Marker environment and from `~/.venvs/pdf-skill`. Setup installs only the light conversion dependencies; install Marker explicitly with `python3.12 scripts/marker_runner.py setup` (and `--full` for its Office extras). See [MarkItDown workflow](references/markitdown.md) for supported formats, routing, image extraction, and failure behavior.

## Verification

Run the offline boundary tests in the light environment:

```bash
~/.venvs/2md/bin/python -m pytest tests -q
```

Tests use generated documents, controlled subprocesses and loopback HTTP servers; they do not establish real model capability. Real conversion smoke checks separately cover OMP `@vision`, an explicit alternate model, Marker native formats/Python/GUI/HTTP, and the unchanged 2pdf handoff. A local OpenAI-compatible model still requires an accessible, installed vision service; a protocol substitute is not local-inference verification.

Analysis text remains escaped verbatim through HTML-to-Markdown conversion, including literal backticks and HTML. It cannot become executable Markdown/HTML or introduce model-provided image URLs.

