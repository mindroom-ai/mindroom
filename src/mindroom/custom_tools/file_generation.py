"""Native file generation toolkit with workspace-confined saving."""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from uuid import uuid4

from mindroom.atomic_file import atomic_write_file_at
from mindroom.llm.media import File
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

logger = get_logger(__name__)

FileFormat = Literal["json", "csv", "pdf", "docx", "html", "txt", "code"]

_WINDOWS_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)
_WINDOWS_PREFIX_RE = re.compile(r"^(?:[A-Za-z]:|\\\\)")

CODE_LANGUAGE_MAP: dict[str, tuple[str, str]] = {
    "python": ("py", "text/x-python"),
    "py": ("py", "text/x-python"),
    "javascript": ("js", "text/javascript"),
    "js": ("js", "text/javascript"),
    "node": ("js", "text/javascript"),
    "typescript": ("ts", "text/plain"),
    "ts": ("ts", "text/plain"),
    "jsx": ("jsx", "text/plain"),
    "tsx": ("tsx", "text/plain"),
    "go": ("go", "text/plain"),
    "golang": ("go", "text/plain"),
    "rust": ("rs", "text/plain"),
    "rs": ("rs", "text/plain"),
    "java": ("java", "text/plain"),
    "kotlin": ("kt", "text/plain"),
    "kt": ("kt", "text/plain"),
    "scala": ("scala", "text/plain"),
    "c": ("c", "text/plain"),
    "cpp": ("cpp", "text/plain"),
    "c++": ("cpp", "text/plain"),
    "csharp": ("cs", "text/plain"),
    "c#": ("cs", "text/plain"),
    "cs": ("cs", "text/plain"),
    "objectivec": ("m", "text/plain"),
    "ruby": ("rb", "text/plain"),
    "rb": ("rb", "text/plain"),
    "php": ("php", "text/plain"),
    "perl": ("pl", "text/plain"),
    "shell": ("sh", "text/plain"),
    "sh": ("sh", "text/plain"),
    "bash": ("sh", "text/plain"),
    "zsh": ("sh", "text/plain"),
    "powershell": ("ps1", "text/plain"),
    "sql": ("sql", "text/plain"),
    "r": ("r", "text/plain"),
    "swift": ("swift", "text/plain"),
    "dart": ("dart", "text/plain"),
    "lua": ("lua", "text/plain"),
    "haskell": ("hs", "text/plain"),
    "elixir": ("ex", "text/plain"),
    "clojure": ("clj", "text/plain"),
    "yaml": ("yaml", "text/plain"),
    "yml": ("yaml", "text/plain"),
    "toml": ("toml", "text/plain"),
    "dockerfile": ("dockerfile", "text/plain"),
}


def _sanitize_filename(filename: str) -> str:
    """Validate and return a safe basename without path components or control characters."""
    normalized = unicodedata.normalize("NFKC", filename)
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        msg = f"Invalid filename: {filename!r}"
        raise ValueError(msg)
    pwp = PureWindowsPath(normalized)
    if _WINDOWS_PREFIX_RE.match(normalized) or pwp.drive or pwp.is_absolute():
        msg = f"Invalid filename: {filename!r}"
        raise ValueError(msg)
    safe = Path(normalized.replace("\\", "/")).name.rstrip(". ")
    if not safe or safe.strip(".") == "" or _WINDOWS_RESERVED_RE.match(safe):
        msg = f"Invalid filename: {filename!r}"
        raise ValueError(msg)
    return safe


def _render_json(content: Any) -> str:  # noqa: ANN401
    if isinstance(content, str):
        try:
            json.loads(content)
        except json.JSONDecodeError:
            return json.dumps({"content": content}, indent=2)
        return content
    return json.dumps(content, indent=2, ensure_ascii=False)


def _render_csv(content: Any, headers: list[str] | None) -> str:  # noqa: ANN401
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content:
        return ""
    output = io.StringIO()
    writer = csv.writer(output)
    if isinstance(content[0], dict):
        fieldnames = list(content[0].keys())
        writer.writerow(fieldnames)
        for row in content:
            if isinstance(row, dict):
                writer.writerow([row.get(field, "") for field in fieldnames])
            else:
                writer.writerow([str(row), *([""] * (len(fieldnames) - 1))])
    elif isinstance(content[0], list):
        if headers:
            writer.writerow(headers)
        writer.writerows(content)
    else:
        if headers:
            writer.writerow(headers)
        for item in content:
            writer.writerow([str(item)])
    return output.getvalue()


def _escape_pdf_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_pdf(content: str, title: str | None) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter  # noqa: PLC0415
        from reportlab.lib.styles import getSampleStyleSheet  # noqa: PLC0415
        from reportlab.lib.units import inch  # noqa: PLC0415
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # noqa: PLC0415
    except ImportError as exc:
        msg = "PDF generation requires reportlab: install with `pip install reportlab`"
        raise RuntimeError(msg) from exc

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=1 * inch)
    styles = getSampleStyleSheet()
    story: list[Any] = []
    if title:
        story.append(Paragraph(_escape_pdf_text(title), styles["Title"]))
        story.append(Spacer(1, 20))
    for para in content.split("\n\n"):
        if stripped := para.strip():
            story.append(Paragraph(_escape_pdf_text(stripped), styles["Normal"]))
            story.append(Spacer(1, 10))
    doc.build(story)
    return buffer.getvalue()


def _render_docx(content: str, title: str | None) -> bytes:
    try:
        from docx import Document  # noqa: PLC0415
    except ImportError as exc:
        msg = "DOCX generation requires python-docx: install with `pip install python-docx`"
        raise RuntimeError(msg) from exc

    document = Document()
    if title:
        document.add_paragraph(title)
    for para in content.split("\n\n"):
        if stripped := para.strip():
            document.add_paragraph(stripped)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _resolve_code_type(language: str | None, filename: str | None) -> tuple[str, str]:
    file_type: str | None = None
    mime_type: str | None = None
    if language:
        normalized = language.strip().lower()
        if normalized in CODE_LANGUAGE_MAP:
            file_type, mime_type = CODE_LANGUAGE_MAP[normalized]
    if filename:
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in basename:
            ext = basename.rsplit(".", 1)[-1].strip().lower()
            if ext and ext != file_type:
                file_type = ext
                mime_type = "text/plain"
    if file_type is None:
        return "txt", "text/plain"
    return file_type, mime_type or "text/plain"


def _render_payload(
    *,
    format: FileFormat,  # noqa: A002
    content: Any,  # noqa: ANN401
    filename: str | None,
    title: str | None,
    headers: list[str] | None,
    language: str | None,
) -> tuple[str | bytes, str, str, str]:
    if format == "json":
        result: tuple[str | bytes, str, str, str] = (_render_json(content), "json", "application/json", "JSON")
    elif format == "csv":
        result = (_render_csv(content, headers), "csv", "text/csv", "CSV")
    else:
        text = content if isinstance(content, str) else json.dumps(content)
        if format == "pdf":
            result = (_render_pdf(text, title), "pdf", "application/pdf", "PDF")
        elif format == "docx":
            result = (
                _render_docx(text, title),
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "DOCX",
            )
        elif format == "html":
            result = (text, "html", "text/html", "HTML")
        elif format == "txt":
            result = (text, "txt", "text/plain", "Text")
        else:
            file_type, mime_type = _resolve_code_type(language, filename)
            result = (text, file_type, mime_type, f"{language or file_type} code")
    return result


class FileGenerationTools(Toolkit):
    """Generate structured, document, web, text, and code files with workspace-confined saving."""

    def __init__(
        self,
        tool_output_workspace_root: Path | None = None,
        enable_json_generation: bool = True,
        enable_csv_generation: bool = True,
        enable_pdf_generation: bool = True,
        enable_docx_generation: bool = True,
        enable_txt_generation: bool = True,
        enable_html_generation: bool = True,
        enable_code_generation: bool = True,
        output_directory: str | None = None,
        save_files: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._relative_output_directory = Path(output_directory or "")
        self.save_files = False
        self.output_directory: Path | None = None

        if save_files or output_directory is not None:
            if tool_output_workspace_root is None:
                logger.warning(
                    "File generation saves are disabled because this toolkit was built without a workspace root",
                )
            elif self._relative_output_directory.is_absolute() or ".." in self._relative_output_directory.parts:
                msg = "file_generation output_directory must be a relative path inside the agent workspace"
                raise ValueError(msg)
            else:
                self.save_files = True
                self.output_directory = tool_output_workspace_root / self._relative_output_directory

        self._enabled_formats: frozenset[str] = frozenset(
            fmt
            for fmt, enabled in (
                ("json", all or enable_json_generation),
                ("csv", all or enable_csv_generation),
                ("pdf", all or enable_pdf_generation),
                ("docx", all or enable_docx_generation),
                ("txt", all or enable_txt_generation),
                ("html", all or enable_html_generation),
                ("code", all or enable_code_generation),
            )
            if enabled
        )
        super().__init__(
            name="file_generation",
            tools=[self.generate_file] if self._enabled_formats else [],
        )

    def _save_file_to_disk(self, content: bytes, filename: str) -> tuple[str | None, str | None]:
        """Publish one sanitized file name in the workspace output directory without following links."""
        assert self._workspace_root is not None
        assert self.output_directory is not None
        try:
            with (
                open_directory_within_root(
                    self._workspace_root,
                    self._relative_output_directory,
                    create=True,
                ) as directory_fd,
                atomic_write_file_at(directory_fd, filename) as file,
            ):
                file.write(content)
        except OSError as error:
            logger.warning("Failed to save generated file", error=str(error))
            return None, str(error)
        return str(self.output_directory / filename), None

    def _build_artifact(
        self,
        payload: str | bytes,
        filename: str | None,
        *,
        file_type: str,
        mime_type: str,
        display_name: str,
    ) -> ToolValue:
        if not filename:
            resolved_name = f"generated_file_{str(uuid4())[:8]}.{file_type}"
        elif not filename.lower().endswith(f".{file_type.lower()}"):
            resolved_name = f"{filename}.{file_type}"
        else:
            resolved_name = filename
        file_name = _sanitize_filename(resolved_name)

        if isinstance(payload, str):
            content_bytes = payload.encode("utf-8")
            count_unit = "characters"
        else:
            content_bytes = payload
            count_unit = "bytes"

        saved_path: str | None = None
        save_error: str | None = None
        if self.save_files and self.output_directory is not None:
            saved_path, save_error = self._save_file_to_disk(content_bytes, file_name)

        # Keep filepath=None so downstream readers always consume the generated bytes rather than a mutable path.
        artifact = File(
            id=str(uuid4()),
            content=content_bytes,
            mime_type=mime_type,
            file_type=file_type,
            filename=file_name,
            size=len(content_bytes),
        )
        summary = f"{display_name} file '{file_name}' generated ({len(payload)} {count_unit})"
        if saved_path:
            summary += f" → {saved_path}"
        elif save_error:
            summary += f" (save failed: {save_error})"
        return ToolValue(content=summary, files=(artifact,))

    def generate_file(
        self,
        format: FileFormat,  # noqa: A002
        content: str | dict[str, Any] | list[Any],
        filename: str | None = None,
        title: str | None = None,
        headers: list[str] | None = None,
        language: str | None = None,
    ) -> ToolValue:
        """Generate a file artifact in the requested format (and save it inside the workspace when configured).

        - `json`: `content` is a dict, list, or JSON string.
        - `csv`: `content` is a list of dicts, list of rows (optional `headers`), or CSV string.
        - `pdf` / `docx`: `content` is text split into paragraphs by blank lines; optional `title`.
        - `html` / `txt`: `content` is raw HTML5 or plain text.
        - `code`: `content` is source code; optional `language` (e.g. `python`, `typescript`, `go`) sets the extension.

        Args:
            format: Output file format.
            content: File payload matching `format`.
            filename: Optional base filename; extension is appended when missing.
            title: Optional document title (`pdf`, `docx`).
            headers: Optional column headers (`csv` list-of-rows input).
            language: Optional programming language (`code`).

        """
        if format not in self._enabled_formats:
            return ToolValue(content=f"Error: '{format}' file generation is disabled in configuration.")
        try:
            payload, file_type, mime_type, display_name = _render_payload(
                format=format,
                content=content,
                filename=filename,
                title=title,
                headers=headers,
                language=language,
            )
            return self._build_artifact(
                payload,
                filename,
                file_type=file_type,
                mime_type=mime_type,
                display_name=display_name,
            )
        except Exception as exc:
            logger.exception("Failed to generate file", format=format)
            return ToolValue(content=f"Error generating {format} file: {exc}")
