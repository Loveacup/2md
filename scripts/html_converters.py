"""Local-only image materialization for HTML, EPUB, and Markdown resources."""
from __future__ import annotations

import base64
import binascii
import mimetypes
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from bs4 import BeautifulSoup
from defusedxml import minidom
from markdown_it import MarkdownIt
from markitdown._stream_info import StreamInfo
from markitdown.converters._epub_converter import EpubConverter

from assets import AssetWriter, FragmentHtmlConverter


def _decode_data_uri(src: str) -> tuple[bytes, str | None]:
    header, separator, payload = src[5:].partition(",")
    if not separator:
        raise ValueError("malformed data URI")
    fields = header.split(";") if header else []
    mime = fields[0].strip().lower() if fields and "/" in fields[0] else None
    if fields and "/" not in fields[0]:
        fields = fields[1:]
    is_base64 = bool(fields and fields[-1].strip().lower() == "base64")
    try:
        data = base64.b64decode(payload, validate=True) if is_base64 else unquote_to_bytes(payload)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("malformed data URI payload") from exc
    if not data:
        raise ValueError("empty data URI image")
    return data, mime


def _relative_resource(src: str) -> str:
    value = src.strip()
    if not value:
        raise ValueError("empty image source")
    if value.lower().startswith("data:"):
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith(("//", "\\\\")):
        raise ValueError("remote or non-local image URL is forbidden")
    decoded = unquote(parsed.path)
    if not decoded or "\x00" in decoded:
        raise ValueError("empty or invalid local image path")
    path = Path(decoded)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", decoded) or decoded.startswith("\\"):
        raise ValueError("absolute image path is forbidden")
    return decoded


class LocalResourceResolver:
    """Resolve data URIs and regular files strictly beneath one source directory."""

    def __init__(self, base: Path):
        self.base = Path(base).resolve(strict=True)
        if not self.base.is_dir():
            raise ValueError(f"resource base is not a directory: {self.base}")

    def resolve(self, src: str) -> tuple[bytes, str | None]:
        if not isinstance(src, str):
            raise ValueError("image source must be text")
        value = src.strip()
        if value.lower().startswith("data:"):
            return _decode_data_uri(value)
        relative = _relative_resource(value)
        candidate = self.base.joinpath(*Path(relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.base)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("missing or out-of-root local image") from exc
        if not resolved.is_file():
            raise ValueError("local image is not a regular file")
        mime = mimetypes.guess_type(resolved.name)[0]
        return resolved.read_bytes(), mime


class _EpubResourceResolver:
    """Resolve an EPUB image against one spine member without extraction."""

    def __init__(self, archive: zipfile.ZipFile, names: set[str], chapter: str):
        self.archive = archive
        self.names = names
        self.chapter = chapter

    def resolve(self, src: str) -> tuple[bytes, str | None]:
        if not isinstance(src, str):
            raise ValueError("EPUB image source must be text")
        value = src.strip()
        if value.lower().startswith("data:"):
            return _decode_data_uri(value)
        relative = _relative_resource(value)
        decoded = relative
        if decoded.startswith("/") or "\\" in decoded:
            raise ValueError("absolute EPUB resource path is forbidden")
        member = posixpath.normpath(posixpath.join(posixpath.dirname(self.chapter), decoded))
        if member in ("", ".", "..") or member.startswith("../") or member.startswith("/"):
            raise ValueError("EPUB resource escapes archive root")
        if member not in self.names:
            raise ValueError(f"missing EPUB image member: {member}")
        mime = mimetypes.guess_type(PurePosixPath(member).name)[0]
        try:
            return self.archive.read(member), mime
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise ValueError(f"cannot read EPUB image member: {member}") from exc


def _replace_html_images(html_text: str, writer: AssetWriter, resolver: object,
                         source_prefix: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for number, image in enumerate(soup.find_all("img", src=True), 1):
        src = image.get("src")
        source_ref = f"{source_prefix}:image:{number}"
        if not src:
            raise ValueError(f"{source_ref}: image has an empty source")
        try:
            data, mime = resolver.resolve(src)
            fragment = writer.add_image(data, mime, source_ref, alt=image.get("alt", ""))
        except Exception as exc:
            raise ValueError(f"{source_ref}: unable to materialize image: {exc}") from exc
        replacement = BeautifulSoup(fragment, "html.parser")
        image.replace_with(*list(replacement.contents))
    return str(soup)


class AssetHtmlConverter(FragmentHtmlConverter):
    """Replace local/data HTML images before delegating HTML-to-Markdown."""

    def __init__(self, writer: AssetWriter, resolver: LocalResourceResolver):
        super().__init__()
        self.writer = writer
        self.resolver = resolver

    def convert(self, file_stream, stream_info: StreamInfo, **kwargs):
        position = file_stream.tell()
        try:
            file_stream.seek(0)
            raw = file_stream.read()
        finally:
            file_stream.seek(position)
        charset = getattr(stream_info, "charset", None) or "utf-8"
        html_text = raw.decode(charset)
        rewritten = _replace_html_images(
            html_text, self.writer, self.resolver, "html"
        )
        return super().convert(
            __import__("io").BytesIO(rewritten.encode("utf-8")),
            StreamInfo(
                mimetype=getattr(stream_info, "mimetype", None),
                extension=getattr(stream_info, "extension", None),
                filename=getattr(stream_info, "filename", None),
                charset="utf-8",
            ),
            **kwargs,
        )


class AssetEpubConverter(EpubConverter):
    """Preserve MarkItDown EPUB metadata/spine order and localize chapter images."""

    def __init__(self, writer: AssetWriter):
        super().__init__()
        self._html_converter = FragmentHtmlConverter()
        self.writer = writer

    def convert(self, file_stream, stream_info: StreamInfo, **kwargs):
        position = file_stream.tell()
        file_stream.seek(0)
        try:
            with zipfile.ZipFile(file_stream, "r") as archive:
                names_list = archive.namelist()
                if len(names_list) != len(set(names_list)):
                    duplicates = sorted({name for name in names_list if names_list.count(name) > 1})
                    raise ValueError(f"EPUB contains duplicate ZIP members: {duplicates}")
                names = set(names_list)
                if "META-INF/container.xml" not in names:
                    raise ValueError("EPUB is missing META-INF/container.xml")
                container = minidom.parse(archive.open("META-INF/container.xml"))
                rootfiles = container.getElementsByTagName("rootfile")
                if not rootfiles:
                    raise ValueError("EPUB container has no rootfile")
                opf_path = unquote(rootfiles[0].getAttribute("full-path"))
                if not opf_path or opf_path.startswith("/"):
                    raise ValueError("EPUB OPF path is invalid")
                opf_path = posixpath.normpath(opf_path)
                if opf_path.startswith("../") or opf_path not in names:
                    raise ValueError(f"EPUB OPF member is missing or outside archive: {opf_path}")
                opf = minidom.parse(archive.open(opf_path))
                metadata = {
                    "title": self._get_text_from_node(opf, "dc:title"),
                    "authors": self._get_all_texts_from_nodes(opf, "dc:creator"),
                    "language": self._get_text_from_node(opf, "dc:language"),
                    "publisher": self._get_text_from_node(opf, "dc:publisher"),
                    "date": self._get_text_from_node(opf, "dc:date"),
                    "description": self._get_text_from_node(opf, "dc:description"),
                    "identifier": self._get_text_from_node(opf, "dc:identifier"),
                }
                manifest: dict[str, str] = {}
                for item in opf.getElementsByTagName("item"):
                    item_id = item.getAttribute("id")
                    href = item.getAttribute("href")
                    if not item_id or not href or item_id in manifest:
                        raise ValueError("EPUB manifest contains an empty or duplicate item id")
                    manifest[item_id] = href
                spine_refs = [item.getAttribute("idref") for item in opf.getElementsByTagName("itemref")]
                if not spine_refs:
                    raise ValueError("EPUB spine is empty")
                opf_base = posixpath.dirname(opf_path)
                spine: list[str] = []
                for item_id in spine_refs:
                    if not item_id or item_id not in manifest:
                        raise ValueError(f"EPUB spine references missing manifest item: {item_id!r}")
                    href = manifest[item_id]
                    parsed = urlsplit(href)
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        raise ValueError("EPUB spine item is not a local member")
                    member = posixpath.normpath(
                        posixpath.join(opf_base, unquote(parsed.path))
                    )
                    if member.startswith("../") or member.startswith("/") or member not in names:
                        raise ValueError(f"EPUB spine member missing or outside archive: {member}")
                    spine.append(member)

                markdown_parts: list[str] = []
                for chapter_number, member in enumerate(spine, 1):
                    extension = PurePosixPath(member).suffix.lower()
                    mimetype = {".html": "text/html", ".xhtml": "application/xhtml+xml"}.get(extension)
                    chapter = archive.read(member)
                    try:
                        html_text = chapter.decode("utf-8")
                    except UnicodeDecodeError:
                        html_text = chapter.decode("utf-8-sig")
                    chapter_resolver = _EpubResourceResolver(archive, names, member)
                    rewritten = _replace_html_images(
                        html_text,
                        self.writer,
                        chapter_resolver,
                        f"epub:chapter:{chapter_number}",
                    )
                    result = self._html_converter.convert(
                        __import__("io").BytesIO(rewritten.encode("utf-8")),
                        StreamInfo(
                            mimetype=mimetype,
                            extension=extension,
                            filename=PurePosixPath(member).name,
                            charset="utf-8",
                        ),
                        **kwargs,
                    )
                    markdown_parts.append(result.markdown.strip())

                metadata_markdown: list[str] = []
                for key, value in metadata.items():
                    if isinstance(value, list):
                        value = ", ".join(value)
                    if value:
                        metadata_markdown.append(f"**{key.capitalize()}:** {value}")
                markdown_parts.insert(0, "\n".join(metadata_markdown))
                from markitdown._base_converter import DocumentConverterResult
                return DocumentConverterResult(
                    markdown="\n\n".join(markdown_parts), title=metadata["title"]
                )
        finally:
            file_stream.seek(position)


class _ImageTagParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0]
        self.line_starts.extend(i + 1 for i, char in enumerate(source) if char == "\n")
        self.images: list[tuple[int, int, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "img":
            return
        line, column = self.getpos()
        start = self.line_starts[line - 1] + column
        text = self.get_starttag_text() or ""
        self.images.append((start, start + len(text), dict(attrs)))

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)


def _source_offset_map(source: str, content: str, line_map) -> list[int]:
    """Map parsed block characters back to original source lines without guessing syntax."""
    if not line_map:
        raise ValueError("cannot locate parsed image in original Markdown")
    raw_lines = source.splitlines(keepends=True)
    content_lines = content.splitlines(keepends=True)
    raw_offsets: list[int] = []
    cursor = 0
    for line in raw_lines:
        raw_offsets.append(cursor)
        cursor += len(line)
    positions = [-1] * (len(content) + 1)
    raw_line_index = line_map[0]
    source_offset = 0
    for content_line in content_lines:
        text = content_line.rstrip("\r\n")
        found = None
        while raw_line_index < line_map[1]:
            raw_line = raw_lines[raw_line_index].rstrip("\r\n")
            column = raw_line.find(text)
            if column >= 0:
                found = (raw_line_index, column)
                break
            raw_line_index += 1
        if found is None:
            raise ValueError("cannot map parsed Markdown block to original source")
        raw_line_index, column = found
        raw_line = raw_lines[raw_line_index]
        for index in range(len(text)):
            positions[source_offset + index] = raw_offsets[raw_line_index] + column + index
        source_offset += len(text)
        newline_length = len(content_line) - len(text)
        for index in range(newline_length):
            positions[source_offset + index] = raw_offsets[raw_line_index] + len(raw_line.rstrip("\r\n")) + index
        source_offset += newline_length
        raw_line_index += 1
    if source_offset != len(content):
        raise ValueError("cannot map parsed Markdown block to original source")
    positions[len(content)] = positions[len(content) - 1] + 1 if content else 0
    return positions


def _capture_inline_spans(parser: MarkdownIt) -> None:
    for name in ("image", "html_inline"):
        rule = next((item for item in parser.inline.ruler.__rules__ if item.name == name), None)
        if rule is None:
            continue
        original = rule.fn
        alt = list(rule.alt)

        def wrapped(state, silent, original=original, name=name):
            start = state.pos
            token_count = len(state.tokens)
            matched = original(state, silent)
            if matched and not silent:
                for token in state.tokens[token_count:]:
                    if token.type == name:
                        token.meta = token.meta or {}
                        token.meta["_2md_source_span"] = (start, state.pos)
            return matched

        parser.inline.ruler.at(name, wrapped, {"alt": alt})


def _resolve_image(writer: AssetWriter, resolver: LocalResourceResolver,
                   source_ref: str, src: str | None, alt: str) -> str:
    if not src:
        raise ValueError(f"{source_ref}: image has an empty source")
    try:
        data, mime = resolver.resolve(src)
        return writer.add_image(data, mime, source_ref, alt=alt)
    except Exception as exc:
        raise ValueError(f"{source_ref}: unable to materialize image: {exc}") from exc


def _html_replacements(content: str, absolute_start: int, offset_map,
                       writer: AssetWriter, resolver: LocalResourceResolver,
                       counter: list[int]) -> list[tuple[int, int, str]]:
    parser = _ImageTagParser(content)
    parser.feed(content)
    replacements = []
    for start, end, attrs in parser.images:
        counter[0] += 1
        source_ref = f"markdown:html-image:{counter[0]}"
        fragment = _resolve_image(
            writer, resolver, source_ref, attrs.get("src"), attrs.get("alt") or ""
        )
        replacements.append(
            (offset_map[absolute_start + start], offset_map[absolute_start + end], fragment)
        )
    return replacements


def materialize_markdown(markdown: str, writer: AssetWriter,
                         resolver: LocalResourceResolver) -> str:
    """Replace parsed image source spans while preserving all other Markdown bytes."""
    parser = MarkdownIt("commonmark", {"html": True})
    _capture_inline_spans(parser)
    tokens = parser.parse(markdown)
    replacements: list[tuple[int, int, str]] = []
    image_number = [0]
    html_number = [0]

    for token in tokens:
        if token.type == "inline" and token.children:
            if not any(child.type == "image" or (child.type == "html_inline" and "<img" in child.content.lower()) for child in token.children):
                continue
            offset_map = _source_offset_map(markdown, token.content, token.map)
            for child in token.children:
                span = (child.meta or {}).get("_2md_source_span")
                if child.type == "image":
                    if not span:
                        raise ValueError("markdown:image: cannot locate parsed image")
                    image_number[0] += 1
                    source_ref = f"markdown:image:{image_number[0]}"
                    fragment = _resolve_image(
                        writer, resolver, source_ref, child.attrGet("src"), child.content or ""
                    )
                    replacements.append((offset_map[span[0]], offset_map[span[1]], fragment))
                elif child.type == "html_inline" and "<img" in child.content.lower():
                    if not span:
                        raise ValueError("markdown:html-image: cannot locate parsed image")
                    replacements.extend(
                        _html_replacements(
                            child.content, span[0], offset_map,
                            writer, resolver, html_number,
                        )
                    )
        elif token.type == "html_block" and "<img" in token.content.lower():
            offset_map = _source_offset_map(markdown, token.content, token.map)
            replacements.extend(
                _html_replacements(
                    token.content, 0, offset_map, writer, resolver, html_number
                )
            )

    replacements.sort(key=lambda item: item[0])
    for previous, current in zip(replacements, replacements[1:]):
        if current[0] < previous[1]:
            raise ValueError("overlapping parsed image source spans")
    for start, end, fragment in reversed(replacements):
        markdown = markdown[:start] + fragment + markdown[end:]
    return markdown


def validate_image_links(markdown: str, root: Path) -> list[Path]:
    """Raise on any invalid image target; return readable, confined image paths."""
    root = Path(root).resolve()
    parser = MarkdownIt("commonmark", {"html": True})
    tokens = parser.parse(markdown)
    targets: list[str] = []

    def collect(items):
        for token in items:
            if token.type == "inline" and token.children:
                collect(token.children)
            elif token.type == "image":
                targets.append(token.attrGet("src") or "")
            elif token.type in ("html_inline", "html_block"):
                html_parser = _ImageTagParser(token.content)
                html_parser.feed(token.content)
                targets.extend(attrs.get("src") or "" for _, _, attrs in html_parser.images)

    collect(tokens)
    paths: list[Path] = []
    for src in targets:
        if not src or src.strip().lower().startswith("data:"):
            raise ValueError("image reference is empty or not a local file")
        parsed = urlsplit(src)
        if (
            parsed.scheme or parsed.netloc or src.startswith(("//", "\\"))
            or re.match(r"^[A-Za-z]:", src)
        ):
            raise ValueError("image reference is not a confined relative path")
        decoded = unquote(parsed.path)
        if not decoded or Path(decoded).is_absolute() or "\\" in decoded:
            raise ValueError("image reference must be a nonempty relative path")
        target = root.joinpath(*Path(decoded).parts)
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise ValueError("image reference is not a regular file")
            with resolved.open("rb") as image_file:
                image_file.read(1)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("image reference is missing, unreadable, or outside output root") from exc
        paths.append(resolved)
    return paths
