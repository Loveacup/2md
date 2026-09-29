# Marker Advanced: Batch, LLM, Python API, GUI, HTTP API

Prerequisite: [Marker standard reference](marker.md) for environment, destinations and output checks. Commands run from the 2md repository root; `R="python3 scripts/marker_runner.py"`. Everything after `--` is passed unchanged upstream.

## 1. Native batch

```bash
$R batch -- INPUT_DIR --output_dir OUT [--workers N] [--max_files N] [--skip_existing] \
         [--num_chunks K --chunk_idx I] [single-file options]
```

Upstream scans only the first directory level, takes every regular file (hidden files included, no extension filter) in unsorted `os.listdir()` order. Failed files are logged and skipped; process exit may still be zero. `--skip_existing` skips if any of `<stem>.md/.html/.json` exists, regardless of requested format. Batch uses an outer worker pool and internally disables per-file multiprocessing; use `--workers 1` for a single worker process. Multi-GPU example: `VLLM_GPUS=0,1 $R batch -- …`; concurrency override: `SURYA_INFERENCE_PARALLEL`.

Use native batch only for a stable, non-empty input directory with supported inputs and unique stems. Otherwise run `single` against a confirmed file list. Shards based on unsorted listing are not guaranteed gap-free; verify actual input/output sets. After batch, inspect every input and report delivered/failed/review lists; no automatic retries. Resume only the identical job with `--skip_existing` after confirming existing output is unchanged. Unified `2md.py convert` instead loops over explicitly confirmed single-file calls and applies its own strict per-document delivery contract.

## 2. Marker LLM enhancement (`--use_llm`)

Off by default. Enabling uploads page images/text to the chosen service; obtain explicit consent for that destination first. Marker LLM is distinct from 2md’s default OMP `@vision` image-analysis workflow. Review actual config and remote inference variables before starting.

| Service (`--llm_service`) | Config fields |
|---|---|
| `marker.services.gemini.GoogleGeminiService` (default) | `gemini_api_key`, `gemini_model_name` |
| `marker.services.vertex.GoogleVertexService` | `vertex_project_id`, `vertex_location`, `gemini_model_name` |
| `marker.services.ollama.OllamaService` | `ollama_base_url`, `ollama_model` |
| `marker.services.claude.ClaudeService` | `claude_api_key`, `claude_model_name` |
| `marker.services.openai.OpenAIService` | `openai_api_key`, `openai_model`, `openai_base_url` |
| `marker.services.azure_openai.AzureOpenAIService` | `azure_endpoint`, `azure_api_key`, `azure_api_version`, `deployment_name` |
| `marker.services.openrouter.OpenRouterService` | `openrouter_api_key`, `openrouter_model`, `openrouter_base_url` |

Upstream reads `GOOGLE_API_KEY` and `OPENROUTER_API_KEY`; other keys are config fields. Never put keys on argv or in repository/example output. Use a mode-600 `--config_json` outside the repo or Python API reading a key from environment. Inspect `--use_llm --help` for related options. Local Surya OCR and optional LLM are different services.

## 3. Python API

Run code in the Marker venv: `$R python -- script.py ARGS`. `JZ2MD_MARKER_PYTHONPATH=/abs/dir1:/abs/dir2` provides trusted custom import paths; this is not a sandbox and is not used by setup/preflight. Parent `PYTHONPATH` is dropped. Interactive REPL is unsupported.

```python
# convert.py — marker_runner.py python -- convert.py IN.pdf OUT_DIR
import os
import sys
from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict, shutdown_models
from marker.output import save_output

src, out_root = sys.argv[1], sys.argv[2]
config = {
    "output_format": "markdown",       # markdown | json | html | chunks
    "output_dir": out_root,             # new output directory
    "mode": "fast",                    # omit for device default
    # "disable_ocr": True,              # no VLM; loses scanned pages/equations
    # "use_llm": True,                  # explicit destination consent required
    # "llm_service": "marker.services.claude.ClaudeService",
    # "claude_api_key": os.environ["ANTHROPIC_API_KEY"],
}
parser = ConfigParser(config)
models = create_model_dict()
try:
    converter = PdfConverter(
        config=parser.generate_config_dict(), artifact_dict=models,
        processor_list=parser.get_processors(), renderer=parser.get_renderer(),
        llm_service=parser.get_llm_service(),
    )
    rendered = converter(src)
    out_dir = parser.get_output_folder(src)
    save_output(rendered, out_dir, parser.get_base_filename(src))
    print(out_dir)
finally:
    shutdown_models(models)
```

Other entry points (same models dict; call `shutdown_models` in `finally`):
- Renderer: `renderer="marker.renderers.chunk.ChunkRenderer"` returns `ChunkOutput` with blocks/html/polygon/bbox/page and `page_info`; JSON/HTML/Markdown/OCR JSON renderer classes are also available.
- Converters: `marker.converters.table.TableConverter` (config `force_layout_block="Table"`), `marker.converters.ocr.OCRConverter` (`keep_chars=True`).
- Blocks: `document = converter.build_document(src)` and `document.contained_blocks((BlockTypes.Form,))`; Marker Form extraction is not PDF form filling (see [2pdf forms](https://github.com/Loveacup/2pdf/blob/main/references/forms.md)).
- Custom processor: subclass `BaseProcessor`; `processor_list` replaces defaults, so extend `PdfConverter.default_processors`. CLI `--processors` likewise replaces defaults.
- Custom renderer/provider: subclass `BaseRenderer` / `BaseProvider`; inspect upstream provider registry before adding a provider.
- Python calls return typed output objects; only `save_output` creates files.

## 4. GUI

```bash
python3.12 scripts/marker_runner.py setup --gui
cd "$(mktemp -d)"
STREAMLIT_SERVER_PORT=8501 python3 /absolute/path/to/2md/scripts/marker_runner.py gui
# open http://127.0.0.1:8501; Ctrl-C stops the app
```

Streamlit must be installed inside Marker venv; the runner does not borrow PATH. It binds to `127.0.0.1` by default. `STREAMLIT_*` config uses environment variables; arguments after `--` are Marker app options. Verified inputs are PDF/images: GUI saves uploads as `temp.pdf` and previews non-PDF pages via PIL, so Office/HTML/EPUB should use CLI/Python. GUI displays results but writes no output files; use `single` for file delivery.

## 5. HTTP API

```bash
python3.12 scripts/marker_runner.py setup --server
cd "$(mktemp -d)"
python3 /absolute/path/to/2md/scripts/marker_runner.py server -- --host 127.0.0.1 --port 8001
```

- `POST /marker` JSON accepts filepath, output format, mode, page range, force OCR and pagination; filepath is read by the server process.
- `POST /marker/upload` accepts multipart `file` and the same optional fields.
- Response is `success`, format/output/images (base64)/metadata or error. Failures also return HTTP 200; inspect `success`.
- API is not a CLI mirror: no `disable_ocr`, `use_llm`, converter or processor selection. Use CLI/Python for those.
- Trusted local use only: no auth or path isolation; filepath reads process-accessible files and uploads land in `./uploads/<client filename>`. Bind localhost; never expose publicly or auto-start as a service.
- Caller owns result saving and must verify images/links.

## 6. Process and inference-service ownership

The runner starts each app (`single`/`batch`/`gui`/`server`/`python`) in its own session/process group and stops only that group: Ctrl-C/SIGTERM sends SIGINT, waits 30 seconds, then SIGTERM, then SIGKILL after 5 seconds. Windows uses CTRL_BREAK_EVENT, then terminates the direct child after 30 seconds; grandchild exit is not confirmed.

Surya inference servers are not runner-owned: explicit `SURYA_INFERENCE_URL`, a server attached through Surya's sentinel file, or a Surya process in a detached session. Surya's keep-alive/atexit rules determine lifetime; shared fast-layout/ocr-error services may remain after app exit. Never kill them by port/process name or delete cache/sentinel files. A venv does not isolate model cache, sentinel files, ports or shared services; Marker installs may share them.
