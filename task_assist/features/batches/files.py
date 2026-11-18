"""批次上传先保留原件，ZIP成员独立入库以免把整包附件关联到错误客户。"""

import hashlib
import uuid
import zipfile
from pathlib import PurePosixPath

from task_assist.features.materials.extraction import extract_text, TEXT_TYPES
from task_assist.features.materials.formats import EXTENSIONS, ZIP, DOCX, XLSX, checked_zip, MAX_EXPANDED


def parse_files(filename, content, content_type):
    filename = filename.replace('\\', '/').split('/')[-1][:200]
    if not filename or filename in {'.', '..'}:
        raise ValueError('文件名无效')
    content_type = (content_type or '').split(';')[0].lower()
    if content_type in {'', 'application/octet-stream', 'application/x-zip-compressed'}:
        content_type = EXTENSIONS.get(PurePosixPath(filename).suffix.lower(), 'text/plain')
    if not content or len(content) > 10 * 1024 * 1024:
        raise ValueError('单文件为空或超过10MiB')
    records, expanded = [], {'bytes': 0, 'count': 0}
    def visit(name, raw, mime, parent=None, depth=0):
        ident = uuid.uuid4().hex
        source = dict(id=ident, parent_id=parent, filename=name[:500], content_type=mime,
            size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), content=raw,
            text_content='', error=None, extraction=None, backend_path=None)
        records.append(source)
        try:
            if mime == ZIP:
                if depth >= 2:
                    raise ValueError('ZIP嵌套过深')
                with checked_zip(raw) as archive:
                    expanded['bytes'] += sum(e.file_size for e in archive.infolist())
                    expanded['count'] += len(archive.infolist())
                    if expanded['bytes'] > MAX_EXPANDED or expanded['count'] > 100:
                        raise ValueError('ZIP展开总量超过限制')
                    for entry in archive.infolist():
                        if not entry.is_dir():
                            mime = EXTENSIONS.get(PurePosixPath(entry.filename).suffix.lower(), 'application/octet-stream')
                            visit(name + '/' + entry.filename, archive.read(entry), mime, ident, depth + 1)
                source['error'] = '压缩包原件；成员已分别识别'  # 容器不送模型，也不整包关联。
            else:
                allowed = TEXT_TYPES | {DOCX, XLSX, 'application/pdf', 'image/png', 'image/jpeg'}
                if mime not in allowed:
                    raise ValueError('不支持的材料格式')
                source['text_content'] = extract_text(raw, mime)
                if not source['text_content'].strip():
                    source['error'] = '没有可提取正文，无法确定归属及材料内容'
        except (ValueError, RuntimeError, KeyError, zipfile.BadZipFile, NotImplementedError) as exc:
            source['error'] = str(exc)[:500]
    visit(filename, content, content_type)
    return records
