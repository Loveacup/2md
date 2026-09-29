# 2md conversion workflow

The standard `convert` command produces one Markdown file, its local image assets, and `conversion.json`. It does not replace Marker’s native advanced interfaces. Read [Vision](vision.md) before enabling analysis or selecting a backend, and [Marker](marker.md) for structured output and advanced parsing.

## Setup and preflight

```bash
python3.12 scripts/2md.py setup
python3 scripts/2md.py preflight --json
```

The light conversion environment is separate from Marker and the 2pdf typesetting venv. Its default is `~/.venvs/2md`; `JZ2MD_VENV` is the sole override. Setup installs the pinned MarkItDown 0.1.8 extra set and image/PDF support into that isolated environment only. Marker dependencies are installed separately with `python3.12 scripts/marker_runner.py setup` (or `setup --full` for Office support). Do not install either parsing engine into `~/.venvs/pdf-skill` or global Python.

`requirements-markitdown.txt` pins MarkItDown and CairoSVG; Pillow, pypdfium2, markdown-it-py, and tinycss2 are direct unpinned dependencies. tinycss2 validates SVG CSS resource references. This is not a complete lockfile: setup runs `pip check`, and real format smoke checks are required after dependency changes.

`preflight` is read-only: it checks the selected environment and requested executable/options, but does not send a model request, start a model or LibreOffice, or install packages. A missing unrequested Marker or vision branch is informational; explicitly selected missing requirements fail preflight. `setup` is idempotent. Python 3.9 can display launcher help/preflight; conversion runtime requires Python >=3.10,<3.15.

## Single-file conversion

```bash
python3 scripts/2md.py convert INPUT --output-dir OUT \
  [--engine auto|markitdown|marker] [--vision on|off] \
  [--vision-backend omp|openai-compatible] [--vision-model MODEL] \
  [--base-url URL] [--api-key-env NAME] [--vision-timeout SECONDS] [--page-images]
```

`auto` accepts existing local regular files. Office and supported text/web formats use MarkItDown; images use the image path. A PDF selects Marker when any page has no extractable text but contains images, lines, curves, or rectangles, including mixed native/scanned documents. Text-bearing pages and genuinely blank pages use MarkItDown. Text that is garbled or structurally complex requires an explicit Marker selection; auto does not guess text quality. This is an initial route, never a fallback after failure. Explicit `--engine marker` supports Marker formats only and needs its separate environment; use its native runner for JSON/HTML/chunks and specialized converters. ZIP files are not unpacked. Audio/video/service-backed APIs remain available through `2md.py python`, with explicit dependency/service setup and upload authorization.

Formats not handled by `auto` fail with guidance to use the native API. Input directories, URLs, missing files, unreadable/encrypted documents fail without modifying or fetching the source. For Office images, XLS legacy has no image callback; native text/table extraction remains available, with that boundary reported. HTML/CSS background images, scripts, video, canvas, and arbitrary remote resources are outside extraction support. HTML and EPUB local image references are resolved only within their permitted input/archive roots. Remote resources and path escapes fail rather than being fetched or silently dropped. Notebook rich outputs are only materialized when the upstream Markdown contains resolvable image references.

## Image and PDF behavior

Embedded image assets are written under `assets/` using content hashes and actual file format extensions. Repeated identical image bytes are stored once while every occurrence keeps its link. Multi-frame GIF/TIFF images are exported frame by frame. SVG source is retained; secure rasterization for vision rejects external resources and active content. Vision analysis does not replace original image assets.

PDF image assets are visible image-region crops rendered at 200 DPI, not original XObject bytes or vector extraction. Pure vector drawings are not promised as extracted images. `--page-images` is PDF-only: it exports full-page 200-DPI previews and, when vision is enabled, analyzes the page image without duplicating crop analysis. It is opt-in to avoid analyzing every digital PDF page by default. Marker may classify drawings as forms or miss assets; an image list is not proof of layout coverage.

## Output and failure contract

A successful run creates `OUT/<input-stem>/<input-stem>.md`, any assets/metadata, and `conversion.json`. The output directory is acquired exclusively; an existing path, including an empty directory or symlink, is a conflict. Use a new OUT root for retries. Input and output must not overlap.

The run stages content under its owned output directory, validates every Markdown/HTML image reference against a real file within the result root, then commits the final Markdown and success report. Completion requires both the final `.md` and `conversion.json` with `status: success`. On failure, exit nonzero, retain a failed report and any `.partial.md`/staging material, and do not leave a success-named Markdown file. Exit 2 indicates invalid invocation/conflict; conversion/vision failure exits 1; Ctrl-C exits 130 and SIGTERM exits 143. A hard-killed `running` report is incomplete. Failures are not automatically retried or rerouted; report the source reference, stage, reason, and remaining unprocessed assets where known.

`conversion.json` records absolute input, engine, vision request/backend/model and endpoint origin (without credentials), per-asset source references and analysis state, errors, and format limitations. It does not store API keys or full model output streams. Keep the entire output directory when passing Markdown to 2pdf: image paths are relative to the Markdown file.

## Marker native path

Use `scripts/marker_runner.py single -- INPUT ...` when native Marker formats, options, or output contracts are needed. The unified converter stages Marker Markdown and metadata, preserves upstream relative image filenames, validates referenced images, and adds an image appendix; it does not rewrite Marker body/table structure or enable `--use_llm`. Read [Marker standard](marker.md) and [Marker advanced](marker-advanced.md) for native options, process ownership and destination checks. The native runner preserves upstream exit semantics; in particular, a successful batch process exit does not prove each input converted.
