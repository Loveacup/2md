# Structured Parsing with Marker

Documents → structure-preserving **Markdown / JSON / HTML / chunks** (layout, reading order, tables, equations, images, OCR). Engine: upstream [Marker v2.0.0](https://github.com/datalab-to/marker/releases/tag/v2.0.0) + Surya 0.22.1, run through `scripts/marker_runner.py` in its own venv. Batch, LLM, Python API, GUI and HTTP API: `references/marker-advanced.md`.

Use this native path when the task needs structure (PDF → Markdown/JSON/chunks, complex/scanned tables, equations, Office/EPUB parsing). Standard Markdown with exported image assets and optional visual transcription uses [`2md.py convert`](markitdown.md). PDF typesetting and editing remain in [2pdf](https://github.com/Loveacup/2pdf/blob/main/SKILL.md).

## 1. Environment

```bash
# From the 2md repository root; Python >=3.10,<4.
python3.12 scripts/marker_runner.py setup              # PDF + images
python3.12 scripts/marker_runner.py setup --full       # + DOCX/PPTX/XLSX/HTML/EPUB (marker-pdf[full])
python3.12 scripts/marker_runner.py setup --gui        # + Streamlit GUI deps
python3.12 scripts/marker_runner.py setup --server     # + FastAPI/uvicorn HTTP deps
python3 scripts/marker_runner.py preflight [--full] [--gui] [--server] [--json]
```

- Default physical venv remains `~/.venvs/2pdf-marker` for compatibility with the already installed environment; override with `JZ2MD_MARKER_VENV`. `JZ2MD_MARKER_PYTHONPATH` provides trusted custom import paths. Old `JZ2PDF_MARKER_*` variables are not supported. Empty paths, the typesetting venv `~/.venvs/pdf-skill`, overlapping parents/children and global Python are refused. Pins are in `scripts/requirements-marker.txt`.
- `setup` never touches the 2pdf typesetting venv, never deletes a directory, never installs Homebrew/Docker/CUDA, and never downloads models. It refuses non-venv non-empty directories and refuses pip when the target interpreter's real `sys.prefix` is not the target venv.
- Missing Marker dependencies → run this Marker `setup`. Do not install Marker in the light MarkItDown venv or retry through another engine.
- `preflight` is read-only: checks venv isolation, exact Marker/Surya versions, selected extras and entrypoints; reports `llama-server`/`docker` PATH and whether `SURYA_INFERENCE_URL` / `FAST_LAYOUT_SERVER_URL` / `OCR_ERROR_SERVER_URL` are set without printing values. It does not verify model cache, service reachability, or WeasyPrint native libraries. Exit 1 = fail; 0 with `overall: degraded` = optional system condition missing.
- Models download on first conversion (network required). OCR, equations and `balanced` mode use a Surya VLM served by an inference server Surya spawns itself: `llama-server` (macOS: `brew install llama.cpp`, or `LLAMA_CPP_BINARY`) on non-NVIDIA machines, vLLM via Docker on NVIDIA.
- `--full` formats render through WeasyPrint → temporary PDF → parse. Missing WeasyPrint native libraries (pango etc.) are reported by conversion; install them per WeasyPrint docs. Office styling/animations/formulas are not lossless.
- Licensing: Marker code is Apache-2.0; **model weights** use modified AI Pubs Open RAIL-M terms ([MODEL_LICENSE](https://github.com/datalab-to/marker/blob/947d7688c0739297a7b9eb08b1a463e3a6853981/MODEL_LICENSE)): commercial use exclusions and attribution duties apply. Flag this for commercial use.

## 2. Before converting: data destination and output directory

Before each run:

1. Check where data goes: CLI args, `--config_json` / Python config (`use_llm`, `llm_service`), and `SURYA_INFERENCE_URL`, `FAST_LAYOUT_SERVER_URL`, `OCR_ERROR_SERVER_URL`. Omitting `--use_llm` does not prove local-only processing. A remote destination without user consent means stop and ask.
2. For native `single`/`batch`, pass `--output_dir` as a new empty directory. Keep previous output and choose a new root on collision. Resume with `--skip_existing` only for the same input/mode/format job.
3. Leave `--mode` unset for upstream device selection (CPU/MPS → `fast`, CUDA → `balanced`). `--disable_ocr` avoids VLM OCR but loses scanned pages/equations; never add it silently after failure.

## 3. Convert one file

```bash
R="python3 scripts/marker_runner.py" # any Python 3; Marker runs in its own venv
$R single -- INPUT --output_format markdown --output_dir OUT # markdown | json | html | chunks
$R single -- --help
$R single -- config --help
```

Everything after `--` goes unchanged to upstream `marker_single`. Common native options:

| Need | Options |
|---|---|
| Pages | `--page_range "0,5-10,20"` (0-based) |
| Speed/quality | `--mode fast\|balanced` |
| OCR | `--force_ocr`, `--strip_existing_ocr`, `--disable_ocr` |
| Inline math in fast mode | `--ocr_inline_math` or `--force_ocr` |
| Images | `--disable_image_extraction` |
| Page headers/footers | `--keep_pageheader_in_output`, `--keep_pagefooter_in_output` |
| Paginated text | `--paginate_output` |
| Extra settings | `--config_json FILE`, `--processors a.B,c.D` |
| Tables only | `--converter_cls marker.converters.table.TableConverter` (+ `--force_layout_block Table`) |
| OCR only | `--converter_cls marker.converters.ocr.OCRConverter` (+ `--keep_chars` for digital PDFs) |

PDF and images need the base install; DOCX/PPTX/XLSX/HTML/EPUB need `setup --full`.

- `TableConverter` emits table blocks (HTML `<table>`; JSON includes page bounding boxes). Scanned tables come from OCR and must be checked separately.
- `OCRConverter` emits OCR JSON (`OCRJSONRenderer`); `--output_format markdown` does not change it. VLM-OCR pages return block HTML without character boxes.
- `--disable_ocr` is not “no models”: the fast layout model still loads.

## 4. Native output acceptance

`single` writes `OUT/<stem>/`: requested `<stem>.md/.html/.json`, `_meta.json` (may be null for OCR output), and relative image files as applicable. Upstream overwrites files in that directory, so always use a new root; different inputs with same stem need separate roots.

A zero exit is not delivery. Verify requested files and metadata exist, inspect expected titles/text/table values/pages, resolve Markdown/HTML image links inside the output root, and report any remote service destination. JSON is a page/block tree; chunks is a flat block list with `page_info`. Recognition/OCR is lossy: report what was checked, not “high accuracy.”

Known Marker 2.0.0 limitations to inspect:
- Vector drawings can become `Form` blocks and be replaced by a VLM description with an empty image link; photo figures may export as `_page_N_Picture_M.jpeg`.
- CJK full-width punctuation may become ASCII.
- HTML tables may flatten; XLSX title rows may merge into table headers.
- Merged date/上午/下午 table labels can disappear on text-layer paths. `--force_ocr` can rebuild labels; Markdown shows labels once with blank cells, while `--force_ocr --output_format html` can preserve `rowspan`/`colspan`. Check merged-cell tables each time.

## 5. File hand-off to 2pdf

```bash
python3 scripts/marker_runner.py single -- in.pdf --output_format markdown --output_dir OUT
python3 /path/to/2pdf/scripts/md2pdf_chrome.py "$(pwd)/OUT/in/in.md" OUT/in-retypeset.pdf --verify
```

Pass the Markdown path within `OUT/<stem>/`; images resolve relative to it. Keep the complete result directory. Re-typesetting is not lossless and the 2pdf renderer does not gain LaTeX math rendering from Marker. The skills exchange files only; neither imports the other's code.

## 6. Errors

| Symptom | Action |
|---|---|
| Marker environment not ready | Run the printed Marker `setup` command |
| Runner exits 2 | Fix the operation or `JZ2MD_MARKER_VENV` / `JZ2MD_MARKER_PYTHONPATH` |
| Upstream traceback/non-zero | Report it; do not switch engines or silently change flags |
| `llama-server binary not found` | Install llama.cpp, set `LLAMA_CPP_BINARY`, or use a user-approved `SURYA_INFERENCE_URL` |
| Model download failure | Network prerequisite; preserve diagnostic, do not disable TLS or switch indexes |
| WeasyPrint/pango errors | Install WeasyPrint native libraries; do not drop requested formats |
