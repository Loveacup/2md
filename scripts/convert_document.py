#!/usr/bin/env python3
"""Native structure plus real assets, committed only after complete validation."""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import runtime

NATIVE = {'.pptx', '.docx', '.xlsx', '.xls', '.html', '.htm', '.epub', '.csv', '.tsv', '.txt', '.md', '.json', '.xml', '.ipynb', '.msg'}
IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.bmp', '.gif', '.svg'}
MARKER = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.bmp', '.gif', '.pptx', '.docx', '.xlsx', '.html', '.htm', '.epub'}
TEXT = {'.txt', '.md', '.csv', '.tsv', '.json', '.xml'}
SCRIPT = Path(__file__).resolve().parent


class UsageError(ValueError):
    pass


class Cancelled(BaseException):
    def __init__(self, signum):
        self.signum = signum


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--engine', choices=('auto', 'markitdown', 'marker'), default='auto')
    p.add_argument('--vision', choices=('on', 'off'), default='on')
    p.add_argument('--vision-backend', choices=('omp', 'openai-compatible'), default='omp')
    p.add_argument('--vision-model')
    p.add_argument('--base-url')
    p.add_argument('--api-key-env')
    p.add_argument('--vision-timeout', type=float, default=300)
    p.add_argument('--page-images', action='store_true')
    return p


def endpoint_origin(value):
    if not value:
        return None
    parsed = urlsplit(value)
    return f'{parsed.scheme}://{parsed.hostname}' + (f':{parsed.port}' if parsed.port else '')


def validate(ns):
    if not ns.input.strip() or not ns.output_dir.strip():
        raise UsageError('Input and output paths must be nonempty')
    source = Path(ns.input).expanduser().resolve()
    if not source.is_file():
        raise UsageError('Input must be an existing local regular file')
    if source.suffix.lower() not in NATIVE | IMAGES | {'.pdf'}:
        raise UsageError('Unsupported convert format; use the explicitly authorized native API through 2md.py python')
    if source.stem in ('', '.', '..'):
        raise UsageError('Input stem must be nonempty and distinct from . and ..')
    if ns.page_images and source.suffix.lower() != '.pdf':
        raise UsageError('--page-images requires a PDF input')
    if not math.isfinite(ns.vision_timeout) or ns.vision_timeout <= 0:
        raise UsageError('--vision-timeout must be a finite positive number')
    if ns.vision_model is not None and not ns.vision_model.strip():
        raise UsageError('--vision-model must be nonempty')
    if ns.base_url is not None:
        try:
            u = urlsplit(ns.base_url)
            valid = u.scheme in ('http', 'https') and u.hostname and not (u.username or u.password or u.query or u.fragment) and u.path.rstrip('/').endswith('/v1')
            u.port
        except ValueError:
            valid = False
        if not valid:
            raise UsageError('--base-url must be an HTTP(S) /v1 root without credentials, query, or fragment')
    if ns.api_key_env is not None and not ns.api_key_env.strip():
        raise UsageError('--api-key-env must name an environment variable')
    if ns.vision == 'on' and ns.vision_backend == 'openai-compatible':
        if not ns.base_url or not ns.vision_model:
            raise UsageError('OpenAI-compatible vision requires explicit --base-url and --vision-model')
        if ns.api_key_env and not os.environ.get(ns.api_key_env):
            raise UsageError('Selected API-key environment variable is empty')
    ns.vision_model = ns.vision_model or ('@vision' if ns.vision_backend == 'omp' else '')
    destination = Path(ns.output_dir).expanduser().resolve() / source.stem
    if destination == source or runtime.is_within(source, destination):
        raise UsageError('Output cannot contain or replace the input')
    if destination.exists() or destination.is_symlink():
        raise UsageError(f'Output already exists; choose a new output directory: {destination}')
    return source, destination


def pdf_route(source):
    import pdfplumber
    needs_ocr = False
    blank = True
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or '').strip()
            visual = bool(page.images or page.lines or page.curves or page.rects)
            blank = blank and not text and not visual
            if not text and visual:
                needs_ocr = True
    return ('marker' if needs_ocr else 'markitdown'), blank


def select_engine(source, requested):
    blank = source.stat().st_size == 0
    if source.suffix.lower() == '.pdf':
        selected, blank = pdf_route(source)
    else:
        selected = 'image' if source.suffix.lower() in IMAGES else 'markitdown'
        if source.suffix.lower() in TEXT:
            blank = not source.read_bytes().strip()
    if requested != 'auto':
        selected = requested
    if selected == 'marker' and source.suffix.lower() not in MARKER:
        raise UsageError('This format is not supported by Marker; use its native provider API explicitly')
    if selected == 'markitdown' and source.suffix.lower() in IMAGES:
        selected = 'image'
    return selected, blank


def atomic_report(path, report):
    temporary = path.with_name('.conversion.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def clean_error(exc, ns):
    # Provider payloads are never logged. Also redact explicit credentials should
    # a dependency include them in an exception.
    message = str(exc)
    if ns.api_key_env and os.environ.get(ns.api_key_env):
        message = message.replace(os.environ[ns.api_key_env], '[REDACTED]')
    if ns.base_url:
        message = message.replace(ns.base_url, endpoint_origin(ns.base_url) or '[endpoint]')
    return message


def native_convert(source, writer):
    from markitdown import MarkItDown, StreamInfo
    from markitdown.converters import PdfConverter
    from office_converters import AssetPptxConverter, AssetDocxConverter, AssetXlsxConverter
    from html_converters import AssetHtmlConverter, AssetEpubConverter, LocalResourceResolver, materialize_markdown
    ext = source.suffix.lower()
    resolver = LocalResourceResolver(source.parent)
    converter = {
        '.pptx': lambda: AssetPptxConverter(writer),
        '.docx': lambda: AssetDocxConverter(writer),
        '.xlsx': lambda: AssetXlsxConverter(writer),
        '.html': lambda: AssetHtmlConverter(writer, resolver),
        '.htm': lambda: AssetHtmlConverter(writer, resolver),
        '.epub': lambda: AssetEpubConverter(writer),
        '.pdf': PdfConverter,
    }.get(ext)
    if converter:
        with source.open('rb') as stream:
            result = converter().convert(stream, StreamInfo(extension=ext, filename=source.name))
        return result.markdown
    if ext in {'.txt', '.md', '.json', '.xml', '.tsv'}:
        # Preserve text/Markdown source verbatim; only image tokens are materialized.
        markdown = source.read_text(encoding='utf-8-sig')
    else:
        markdown = MarkItDown(enable_plugins=False).convert(str(source)).markdown
    return materialize_markdown(markdown, writer, resolver)


def pdf_images(source, writer):
    import pdfplumber
    fragments = []
    with pdfplumber.open(source) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            for i, image in enumerate(sorted(page.images, key=lambda im: (im['top'], im['x0'])), 1):
                ref = f'pdf:page:{n}/image:{i}'
                bounds = page.bbox
                bbox = (max(bounds[0], image['x0']), max(bounds[1], image['top']), min(bounds[2], image['x1']), min(bounds[3], image['bottom']))
                if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                    raise RuntimeError(f'{ref}: image has no visible intersection with its page')
                try:
                    raster = page.crop(bbox).to_image(resolution=200).original
                    data = BytesIO(); raster.save(data, 'PNG')
                    fragment = writer.add_image(data.getvalue(), 'image/png', ref, f'第 {n} 页图片 {i}')
                except Exception as exc:
                    raise RuntimeError(f'{ref}: {exc}') from exc
                fragments.append(f'\n\n### 第 {n} 页图片 {i}\n\n{fragment}')
    return ''.join(fragments)


def page_images(source, writer):
    import pypdfium2 as pdfium
    fragments = []
    with pdfium.PdfDocument(source) as pdf:
        for n in range(len(pdf)):
            page = pdf[n]
            try:
                bitmap = page.render(scale=200 / 72)
                try:
                    raster = bitmap.to_pil()
                    data = BytesIO(); raster.save(data, 'PNG')
                finally:
                    bitmap.close()
            finally:
                page.close()
            fragment = writer.add_image(data.getvalue(), 'image/png', f'pdf:page:{n+1}/preview', f'第 {n+1} 页整页预览')
            fragments.append(f'\n\n### 第 {n+1} 页整页预览 / 转写 / 描述\n\n{fragment}')
    return ''.join(fragments)


def marker_preflight():
    proc = subprocess.run([sys.executable, str(SCRIPT / 'marker_runner.py'), 'preflight', '--json'], capture_output=True, text=True, env=runtime.mgmt_env(), timeout=120)
    if proc.returncode:
        raise RuntimeError('Marker environment is not ready; run marker_runner.py preflight --json and setup separately')


def metadata_errors(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('error', 'errors') and item:
                return True
            if metadata_errors(item):
                return True
    elif isinstance(value, list):
        return any(metadata_errors(item) for item in value)
    return False


def marker_convert(source, stage, writer):
    from html_converters import validate_image_links
    code = runtime.run_owned([sys.executable, str(SCRIPT / 'marker_runner.py'), 'single', '--', str(source), '--output_format', 'markdown', '--output_dir', str(stage)], runtime.mgmt_env())
    if code in (130, 143):
        raise Cancelled(code - 128)
    if code:
        raise RuntimeError(f'Marker exited {code}; no fallback was attempted')
    root = stage / source.stem
    main = root / f'{source.stem}.md'
    meta = root / f'{source.stem}_meta.json'
    if not main.is_file() or not meta.is_file():
        raise RuntimeError('Marker returned without required Markdown and metadata')
    markdown = main.read_text(encoding='utf-8')
    main.rename(root / f'{source.stem}.partial.md')
    if metadata_errors(json.loads(meta.read_text(encoding='utf-8'))):
        raise RuntimeError('Marker metadata contains conversion errors')
    linked = validate_image_links(markdown, root)
    images = set(linked)
    images.update(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in IMAGES)
    fragments = []
    for image in sorted(images):
        relative = image.relative_to(root).as_posix()
        fragment = writer.add_existing_image(image, f'marker:image:{relative}', relative)
        fragments.append(f'\n\n### 图片：{relative}\n\n{fragment}')
    return markdown + ''.join(fragments)


def limitations_for(source, ns):
    ext = source.suffix.lower()
    limits = []
    if ns.vision == 'off':
        limits.append('Vision is off: embedded screenshots are exported without visual transcription or description; Marker local OCR is independent.')
    if ext == '.pptx':
        limits.append('Embedded pictures only; SmartArt, native shapes and theme backgrounds are not whole-slide renders. Charts retain native data. Export PDF and request --page-images for full-page layout.')
    if ext in ('.xlsx', '.xls'):
        limits.append('XLSX images follow their sheet table, not their original cell; legacy XLS has no embedded image hook.')
    if ext in ('.html', '.htm', '.epub'):
        limits.append('Only image elements are materialized; CSS backgrounds, scripts, video and canvas are outside the extraction contract.')
    if ext == '.ipynb':
        limits.append('Only upstream-emitted image references are materialized; export rich outputs/attachments to HTML when missing.')
    if ext == '.pdf':
        limits.append('PDF image regions are visible 200-DPI crops, not original XObject bytes; independent vector graphics are not enumerated. Marker may classify pictures as Form blocks.')
        if ns.page_images:
            limits.append('Visual results target complete pages; extracted assets remain saved but are not separately analyzed.')
    return limits


def main(argv=None):
    ns = parser().parse_args(argv)
    try:
        source, destination = validate(ns)
        engine, blank = select_engine(source, ns.engine)
        if engine == 'marker':
            marker_preflight()
    except UsageError as exc:
        print(f'usage: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'preflight: {clean_error(exc, ns)}', file=sys.stderr)
        return 1
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        print(f'usage: Output already exists: {destination}', file=sys.stderr)
        return 2
    except OSError as exc:
        print(f'output: {exc}', file=sys.stderr)
        return 1
    report = {'status': 'running', 'input': str(source), 'engine': engine,
              'vision': {'enabled': ns.vision == 'on', 'backend': ns.vision_backend,
                         'requested_model': ns.vision_model, 'actual_model': [],
                         'endpoint': endpoint_origin(ns.base_url)},
              'assets': [], 'errors': [], 'limitations': limitations_for(source, ns)}
    stage = None
    writers = []
    phase = 'initialization'
    final = destination / f'{source.stem}.md'
    partial = destination / f'{source.stem}.partial.md'
    previous = {}
    exit_code = 1
    try:
        atomic_report(destination / 'conversion.json', report)
        stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=destination))
        staged = stage / source.stem
        staged.mkdir()
        staged_partial = staged / f'{source.stem}.partial.md'
        staged_partial.write_text('', encoding='utf-8')
        from assets import AssetWriter
        from html_converters import validate_image_links
        def make_writer(enabled):
            writer = AssetWriter(staged, vision=enabled, backend=ns.vision_backend, model=ns.vision_model,
                                 base_url=ns.base_url, api_key_env=ns.api_key_env, timeout=ns.vision_timeout)
            writers.append(writer)
            return writer
        def interrupted(signum, _frame):
            raise Cancelled(signum)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
        writer = make_writer(ns.vision == 'on' and not ns.page_images)
        phase = 'conversion'
        if engine == 'image':
            markdown = writer.add_image(source.read_bytes(), None, 'image:frame:1', source.stem)
        elif engine == 'marker':
            markdown = marker_convert(source, stage, writer)
        else:
            markdown = native_convert(source, writer)
            staged_partial.write_text(markdown, encoding='utf-8')
            if source.suffix.lower() == '.pdf':
                markdown += pdf_images(source, writer)
        staged_partial.write_text(markdown, encoding='utf-8')
        if ns.page_images:
            phase = 'page-images'
            markdown += page_images(source, make_writer(ns.vision == 'on'))
        staged_partial.write_text(markdown, encoding='utf-8')
        phase = 'validation'
        report['assets'] = [entry for item in writers for entry in item.entries]
        if not markdown.strip() and not blank and not report['assets']:
            raise RuntimeError('Nonblank input produced no text or assets')
        validate_image_links(markdown, staged)
        if any(item.errors for item in writers):
            raise RuntimeError('Asset extraction or analysis reported errors')
        if any(entry['analysis_status'] in ('pending', 'failed') for entry in report['assets']):
            raise RuntimeError('Not every discovered image completed delivery')
        report['vision']['actual_model'] = sorted({entry['actual_model'] for entry in report['assets'] if entry.get('actual_model')})
        phase = 'commit'
        for item in list(staged.iterdir()):
            target = destination / item.name
            if target.exists() or target.is_symlink():
                raise RuntimeError(f'Unexpected output collision: {item.name}')
            item.rename(target)
        partial.replace(final)
        report['status'] = 'success'
        atomic_report(destination / 'conversion.json', report)
        print(final)
        return 0
    except (Exception, Cancelled, KeyboardInterrupt) as exc:
        if isinstance(exc, Cancelled):
            exit_code = 128 + exc.signum
            message = f'Cancelled by signal {exc.signum}'
        elif isinstance(exc, KeyboardInterrupt):
            exit_code = 128 + getattr(exc, "signum", signal.SIGINT)
            message = f'Cancelled by signal {exit_code - 128}'
        else:
            message = clean_error(exc, ns)
        if final.exists():
            final.replace(partial)
        report['status'] = 'failed'
        report['assets'] = [entry for item in writers for entry in item.entries]
        report['errors'] = [error for item in writers for error in item.errors]
        report['errors'].append({'source_ref': getattr(exc, 'source_ref', str(source)), 'stage': phase, 'message': message})
        report['unprocessed_assets'] = sum(entry.get('analysis_status') == 'pending' for entry in report['assets'])
        try:
            atomic_report(destination / 'conversion.json', report)
        except OSError as report_error:
            print(f'Unable to persist failed report: {report_error}', file=sys.stderr)
        print(f'{phase}: {message}; staging: {stage or destination}', file=sys.stderr)
        return exit_code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    sys.exit(main())
