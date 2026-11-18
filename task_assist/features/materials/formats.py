"""有界读取复合材料，提取文本保留页码、工作表和压缩包成员定位。"""

import io
import zipfile
from pathlib import PurePosixPath

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ZIP = "application/zip"
EXTENSIONS = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown",
              ".csv": "text/csv", ".json": "application/json", ".pdf": "application/pdf",
              ".docx": DOCX, ".xlsx": XLSX, ".zip": ZIP}
MAX_EXPANDED = 12 * 1024 * 1024
MAX_ENTRIES = 100


def checked_zip(content):
    archive = zipfile.ZipFile(io.BytesIO(content))
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_EXPANDED:
        archive.close()
        raise ValueError("压缩文件展开后过大或成员超过 100 项")
    for entry in entries:
        path = PurePosixPath(entry.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or entry.flag_bits & 1:
            archive.close()
            raise ValueError("压缩文件含不安全路径或加密成员")
    return archive


def pdf_text(content):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请先解密")
        if len(reader.pages) > 100:
            raise ValueError("PDF 超过 100 页，请拆分")
        parts = []
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"[PDF 第 {index} 页]\n{text}")
            if sum(map(len, parts)) > 60000:
                raise ValueError("资料正文超过 60000 字，请拆分后上传")
        return "\n".join(parts)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 正文格式无效或无法提取") from exc


def excel_text(content):
    from openpyxl import load_workbook
    workbook = None
    try:
        with checked_zip(content):
            pass
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        parts, length, cells = [], 0, 0
        for sheet in workbook:
            for row in sheet.iter_rows():
                for cell in row:
                    cells += 1
                    if cells > 100000:
                        raise ValueError("Excel 单元格过多，请拆分")
                    if cell.value is None:
                        continue
                    # 公式以原始字符串交给模型核对，不计算，不访问外部链接。
                    line = f"[{sheet.title}!{cell.coordinate}] {cell.value}"
                    length += len(line) + 1
                    if length > 60000:
                        raise ValueError("资料正文超过 60000 字，请拆分后上传")
                    parts.append(line)
        return "\n".join(parts)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Excel 正文格式无效") from exc
    finally:
        if workbook:
            workbook.close()


def archive_text(content, extract, depth, budget):
    if depth >= 2:
        raise ValueError("ZIP 嵌套超过两层，请拆包上传")
    with checked_zip(content) as archive:
        budget["bytes"] += sum(e.file_size for e in archive.infolist())
        budget["entries"] += len(archive.infolist())
        if budget["bytes"] > MAX_EXPANDED or budget["entries"] > MAX_ENTRIES:
            raise ValueError("ZIP 嵌套展开总量超过限制")
        parts, length = [], 0
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            content_type = EXTENSIONS.get(PurePosixPath(entry.filename).suffix.lower())
            if content_type is None:
                text = "[未提取正文：不支持的成员格式，请人工核对原件]"
            else:
                try:
                    text = extract(archive.read(entry), content_type, _depth=depth + 1, _budget=budget)
                except ValueError:
                    text = "[未提取正文：成员损坏、编码异常或超过读取限制，请人工核对原件]"
                if not text:
                    text = "[未提取正文：扫描件或空白资料，请人工核对原件]"
            part = f"[ZIP 成员：{entry.filename}]\n{text}"
            length += len(part) + 1
            if length > 60000:
                raise ValueError("资料正文超过 60000 字，请拆包分批上传")
            parts.append(part)
        return "\n".join(parts)
