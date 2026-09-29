"""Content-addressed image export and vision annotation for one conversion."""
from __future__ import annotations

import hashlib
import html
import io
import uuid
import urllib.parse
from defusedxml import ElementTree as ET
from pathlib import Path
from typing import Any
from markitdown import DocumentConverterResult, StreamInfo
from markitdown.converters._html_converter import HtmlConverter


class FragmentHtmlConverter(HtmlConverter):
    """Keep untrusted analysis verbatim through upstream HTML-to-Markdown."""

    def convert(self, file_stream, stream_info, **kwargs):
        from bs4 import BeautifulSoup
        position = file_stream.tell()
        try:
            raw = file_stream.read().decode(stream_info.charset or "utf-8")
        finally:
            file_stream.seek(position)
        soup = BeautifulSoup(raw, "html.parser")
        annotations = {}
        for block in soup.select("pre[data-2md-verbatim]"):
            token = "JZ2MDANNOTATION" + uuid.uuid4().hex + "END"
            annotations[token] = "<pre>" + html.escape(block.get_text(), quote=True) + "</pre>"
            block.replace_with(token)
        kwargs["strict"] = True
        result = super().convert(io.BytesIO(str(soup).encode("utf-8")),
                                 StreamInfo(charset="utf-8"), **kwargs)
        markdown = result.markdown
        for token, safe_html in annotations.items():
            markdown = markdown.replace(token, safe_html)
        return DocumentConverterResult(markdown=markdown, title=result.title)


class AssetError(ValueError):
    def __init__(self, source_ref: str, message: str):
        self.source_ref = source_ref
        super().__init__(f"{source_ref}: {message}")


class AssetWriter:
    """Save each distinct image once while retaining every occurrence."""

    def __init__(self, root: Path, *, vision: bool, backend: str = "omp",
                 model: str = "@vision", base_url: str | None = None,
                 api_key_env: str | None = None, timeout: float = 300,
                 analyze: bool = True):
        self.root = Path(root).resolve()
        self.vision = vision
        self.backend = backend
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.analyze = analyze
        self.entries: list[dict[str, Any]] = []
        self.errors: list[dict[str, str]] = []
        self._cache: dict[tuple, Any] = {}
        self._by_path: dict[str, dict] = {}

    def add_image(self, data: bytes, mime: str | None, source_ref: str,
                  alt: str = "") -> str:
        try:
            from PIL import Image, ImageSequence

            data = bytes(data)
            if _is_svg(data, mime):
                _validate_svg(data)
                analysis_data = None
                if self.vision and self.analyze:
                    import cairosvg
                    analysis_data = cairosvg.surface.PNGSurface.convert(
                        bytestring=data, unsafe=False, url_fetcher=_svg_fetcher)
                return self._register(data, ".svg", "image/svg+xml", source_ref, alt,
                                      analysis_data=analysis_data)

            with Image.open(io.BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                suffix, detected_mime = _image_type(image_format)
                image.load()
                if image_format in {"GIF", "TIFF", "TIF"} and getattr(image, "n_frames", 1) > 1:
                    frames = []
                    for frame_number, frame in enumerate(ImageSequence.Iterator(image), 1):
                        output = io.BytesIO()
                        frame = frame.copy()
                        frame.convert("RGBA" if "A" in frame.getbands() else "RGB").save(output, format="PNG")
                        frames.append((output.getvalue(), f"{source_ref}/frame:{frame_number}"))
                    return "".join(self._register(frame, ".png", "image/png", ref, alt,
                                                  analysis_data=frame)
                                   for frame, ref in frames)
            return self._register(data, suffix, detected_mime, source_ref, alt,
                                  analysis_data=data)
        except Exception as exc:
            self._fail(source_ref, exc)

    def add_existing_image(self, path: Path, source_ref: str, alt: str = "") -> str:
        """Register an already-exported Marker asset without changing its name."""
        try:
            candidate = Path(path)
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
            if not resolved.is_file():
                raise ValueError("existing asset is not a regular file")
            data = resolved.read_bytes()
            mime = _mime_for_suffix(resolved.suffix)
            if mime == "image/svg+xml":
                _validate_svg(data)
                analysis_data = None
                if self.vision and self.analyze:
                    import cairosvg
                    analysis_data = cairosvg.surface.PNGSurface.convert(
                        bytestring=data, unsafe=False, url_fetcher=_svg_fetcher)
            else:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                analysis_data = data
            return self._register(data, resolved.suffix.lower(), mime, source_ref,
                                  alt, path=resolved, analysis_data=analysis_data)
        except Exception as exc:
            self._fail(source_ref, exc)

    def _register(self, data: bytes, suffix: str, mime: str, source_ref: str,
                  alt: str, *, analysis_data: bytes | None = None,
                  path: Path | None = None) -> str:
        digest = hashlib.sha256(data).hexdigest()
        if path is None:
            rel = f"assets/{digest}{suffix}"
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
        else:
            try:
                rel = path.relative_to(self.root).as_posix()
            except ValueError:
                raise ValueError("asset path escapes output root")
        entry = self._by_path.get(rel)
        if entry is None:
            entry = {"path": rel, "source_refs": [],
                     "analysis_status": "pending" if self.vision and self.analyze else "disabled"}
            self._by_path[rel] = entry
            self.entries.append(entry)
        if source_ref not in entry["source_refs"]:
            entry["source_refs"].append(source_ref)
        if self.vision and self.analyze:
            try:
                from vision import analyze_image
                key = (hashlib.sha256(data).hexdigest(),
                       self.backend, self.model, self.base_url, "2md-vision-v1")
                result = self._cache.get(key)
                if result is None:
                    analysis_suffix = ".png" if suffix == ".svg" else suffix
                    analysis_path = self.root / (".analysis-image" + analysis_suffix)
                    analysis_path.write_bytes(analysis_data if analysis_data is not None else data)
                    try:
                        result = analyze_image(analysis_path, backend=self.backend,
                                               model=self.model, base_url=self.base_url,
                                               api_key_env=self.api_key_env, timeout=self.timeout)
                    finally:
                        analysis_path.unlink(missing_ok=True)
                    self._cache[key] = result
                entry.update(analysis_status="complete", actual_model=result.model,
                             transcription=result.transcription, description=result.description)
            except Exception as exc:
                entry["analysis_status"] = "failed"
                self._fail(source_ref, exc)
        shown_alt = alt or ""
        transcription = entry.get("transcription")
        description = entry.get("description")
        safe_path = html.escape(urllib.parse.quote(rel, safe="/._-"), quote=True)
        bits = [f'<img src="{safe_path}" alt="{html.escape(shown_alt, quote=True)}">']
        if transcription is not None:
            bits.append(f'<p><strong>图片文字转写</strong></p><pre data-2md-verbatim="true">{html.escape(transcription, quote=True)}</pre>')
        if description is not None:
            bits.append(f'<p><strong>图片描述</strong></p><pre data-2md-verbatim="true">{html.escape(description, quote=True)}</pre>')
        return "\n".join(bits)

    def _fail(self, source_ref: str, exc: Exception) -> None:
        if isinstance(exc, AssetError):
            raise exc
        message = str(exc)
        if self.api_key_env:
            import os
            secret = os.environ.get(self.api_key_env)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        self.errors.append({"source_ref": source_ref, "stage": "asset", "message": message})
        if not any(source_ref in entry["source_refs"] for entry in self.entries):
            self.entries.append({"path": "", "source_refs": [source_ref], "analysis_status": "failed"})
        raise AssetError(source_ref, message) from exc


def _is_svg(data: bytes, mime: str | None) -> bool:
    return (mime or "").lower().split(";", 1)[0].strip() == "image/svg+xml" or data.lstrip().startswith(b"<svg") or b"<svg" in data[:512]


def _validate_svg(data: bytes, depth: int = 0) -> None:
    """Reject DTDs, entities, active content, and non-data resource references."""
    if depth > 8:
        raise ValueError("SVG nested data resources exceed safe depth")
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"<?XML-STYLESHEET" in upper:
        raise ValueError("SVG DTD/entity declarations are forbidden")
    root = ET.fromstring(data)
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("SVG root element is required")
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "animate", "animatetransform", "animatemotion", "set"}:
            raise ValueError(f"active SVG element forbidden: {tag}")
        if tag == "style":
            _validate_css(node.text or "", depth)
        for key, value in node.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                raise ValueError("SVG event handlers are forbidden")
            if name in {"href", "src"}:
                _validate_svg_resource(value, depth)
            if name == "style" or "url" in value.lower() or "\\" in value:
                _validate_css(value, depth)


def _validate_svg_resource(value: str, depth: int = 0) -> None:
    value = value.strip()
    if not value or value.startswith("#"):
        return
    if not value.lower().startswith("data:image/"):
        raise ValueError("external SVG resources are forbidden")
    data = _svg_fetcher(value)
    if _is_svg(data, value[5:].split(",", 1)[0]):
        _validate_svg(data, depth + 1)


def _validate_css(value: str, depth: int = 0) -> None:
    import tinycss2
    def visit(tokens):
        for token in tokens:
            if token.type == "at-keyword" and token.value.lower() == "import":
                raise ValueError("SVG CSS imports are forbidden")
            if token.type == "url":
                _validate_svg_resource(token.value, depth)
            elif token.type == "function":
                if token.lower_name == "url":
                    inner = [part for part in token.arguments if part.type not in ("whitespace", "comment")]
                    if len(inner) != 1 or inner[0].type not in ("string", "url", "ident", "hash"):
                        raise ValueError("invalid SVG CSS resource")
                    resource = ("#" if inner[0].type == "hash" else "") + inner[0].value
                    _validate_svg_resource(resource, depth)
                else:
                    visit(token.arguments)
            elif hasattr(token, "content"):
                visit(token.content)
    visit(tinycss2.parse_component_value_list(value))


def _svg_fetcher(url: str, resource_type: str = "image/svg+xml") -> bytes:
    if url.lower().startswith("data:"):
        header, payload = url.split(",", 1)
        if ";base64" in header.lower():
            import base64
            return base64.b64decode(payload, validate=True)
        return urllib.parse.unquote_to_bytes(payload)
    raise ValueError("external SVG resource fetch is forbidden")


def _image_type(fmt: str) -> tuple[str, str]:
    types = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
             "WEBP": (".webp", "image/webp"), "BMP": (".bmp", "image/bmp"),
             "GIF": (".gif", "image/gif"), "TIFF": (".tiff", "image/tiff")}
    try:
        return types[fmt]
    except KeyError:
        raise ValueError(f"unsupported or unknown image format: {fmt or 'unknown'}")


def _mime_for_suffix(suffix: str) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".tif": "image/tiff",
            ".tiff": "image/tiff", ".bmp": "image/bmp", ".svg": "image/svg+xml"}.get(suffix.lower(), "application/octet-stream")
