"""Read-only local HTML discovery and metadata extraction; standard library only."""
from __future__ import annotations

import hashlib
import codecs
import errno
import os
import re
import stat
import time
from html.parser import HTMLParser
from pathlib import Path

SKIP_DIRS = {"node_modules", "vendor", "venv", "__pycache__", "Library", "Applications",
             "Caches", "cache", "dist", "build", "site-packages", "backups", "TimeMachine"}
MAX_BYTES = 4 * 1024 * 1024
MAX_TEXT = 100_000
TEXT_BOUNDARIES = {'address', 'article', 'aside', 'blockquote', 'body', 'br', 'caption',
                   'dd', 'details', 'dialog', 'div', 'dl', 'dt', 'fieldset', 'figcaption',
                   'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                   'head', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre',
                   'section', 'summary', 'table', 'tbody', 'td', 'tfoot', 'th',
                   'thead', 'title', 'tr', 'ul'}


class CatalogExport(ValueError):
    """Manager-generated catalogs are not source documents."""


def excluded_directory_name(name: str) -> bool:
    return (name.removesuffix('.nosync') in SKIP_DIRS
            or name.endswith(('.app', '.framework', '.bundle')))


def allowed_path(path: Path) -> bool:
    """Reject sensitive names, hidden components, and any symlink component."""
    for part in path.parts:
        low = part.lower()
        if part.startswith('.') or low.startswith(('credentials.', 'secrets.', 'id_rsa')):
            return False
        if low in {'auth.json', 'credentials', 'secrets'} or low.endswith(('.key', '.pem')):
            return False
    return not any(p.is_symlink() for p in [path, *path.parents])


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip: list[str] = []
        self.in_title = False
        self.heading: str | None = None
        self.titles: list[str] = []
        self.headings: list[list[str]] = []
        self.text: list[str] = []
        self.length = 0
        self.vector_depth = 0
        self.has_title = False

    def text_boundary(self):
        if self.text and not self.text[-1].endswith(' ') and self.length < MAX_TEXT:
            self.text.append(' ')
            self.length += 1
            if self.heading:
                self.headings[-1].append(' ')

    def handle_starttag(self, tag, attrs):
        if tag in {'svg', 'math'}:
            self.vector_depth += 1
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.skip.append(tag)
        if self.skip:
            return
        if tag in TEXT_BOUNDARIES:
            self.text_boundary()
        if tag == 'title' and not self.vector_depth and not self.has_title:
            self.in_title = True
        if tag in {'h1', 'h2', 'h3'}:
            self.heading = tag
            self.headings.append([])

    def handle_endtag(self, tag):
        if tag in {'svg', 'math'}:
            self.vector_depth = max(0, self.vector_depth - 1)
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == 'title':
            if self.in_title:
                self.has_title = True
            self.in_title = False
        if tag == self.heading:
            self.heading = None
        if tag in TEXT_BOUNDARIES:
            self.text_boundary()

    def handle_data(self, data):
        if self.skip or self.length >= MAX_TEXT:
            return
        # Inline tags do not add spaces: 人工<span>智能</span> remains 人工智能.
        # Keep source whitespace; block tags add their own bounded separator.
        text = re.sub(r'\s+', ' ', data)
        if not text:
            return
        text = text[:MAX_TEXT - self.length]
        self.length += len(text)
        self.text.append(text)
        if self.in_title:
            self.titles.append(text)
        if self.heading:
            self.headings[-1].append(text)


def clean_title(value: str) -> str:
    value = re.sub(r'^\d{8}[-_ ]\d{4}[-_ ]*', '', value)
    value = re.sub(r'^\d{4}[-/]\d{2}[-/]\d{2}\s*', '', value)
    return re.sub(r'\s+', ' ', value.replace('_', ' ')).strip(' -·|')


def short_label(title: str, headings: list[str], filename: str) -> tuple[str, str]:
    """Extractive, traceable topic labels rather than invented semantic claims."""
    candidates = [(clean_title(title), '页面标题'),
                  *[(clean_title(h), '页面小标题') for h in headings[:4]],
                  (clean_title(Path(filename).stem), '文件名')]
    generic = {'index', 'untitled', 'document', 'home', '首页', '无标题'}
    for candidate, basis in candidates:
        if len(candidate) < 3 or candidate.casefold() in generic:
            continue
        # Prefer a meaningful first clause; otherwise truncate without disguising extraction.
        clause = re.split(r'[|｜\n：:—·]', candidate)[0].strip()
        if len(clause) < 4:
            clause = candidate
        if len(clause) > 20:
            clause = clause[:19].rstrip() + '…'
        return clause, basis
    return clean_title(Path(filename).stem)[:20] or '未命名页面', '文件名'


def extract(path: Path) -> dict:
    if not allowed_path(path):
        raise ValueError('不允许索引隐藏、敏感或符号链接路径')
    if getattr(path.stat(), 'st_flags', 0) & getattr(stat, 'SF_DATALESS', 0):
        raise OSError(errno.EAGAIN, '云端文件仅有占位符；未下载正文')
    # O_NOFOLLOW and regular-file check prevent devices/FIFOs and final symlink races.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('不是普通文件')
        prefix = stream.read(8192)
        if b'name="html-manager-catalog" content="true"' in prefix:
            raise CatalogExport('管理器目录导出文件不重复索引')
        raw = prefix + stream.read(MAX_BYTES - len(prefix))
    truncated = info.st_size > MAX_BYTES
    encoding = 'utf-8-sig'
    match = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', raw[:4096], re.I)
    if match:
        declared = match[1].decode('ascii').lower()
        if declared in {'utf-8', 'gb2312', 'gbk', 'gb18030', 'big5', 'utf-16', 'windows-1252', 'iso-8859-1', 'latin-1'}:
            encoding = 'gb18030' if declared in {'gbk', 'gb2312'} else declared
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encoding = 'utf-16'
    try:
        html = codecs.getincrementaldecoder(encoding)(errors='strict').decode(raw, final=not truncated)
    except UnicodeDecodeError:
        html = codecs.getincrementaldecoder('gb18030')(errors='replace').decode(raw, final=not truncated)
        encoding = 'gb18030-fallback'
    parser = TextExtractor()
    parser.feed(html)
    parser.close()  # Flush text buffered at EOF, including an unterminated entity.
    page_title = clean_title(''.join(parser.titles))
    title = page_title or clean_title(path.stem)
    text = ' '.join(''.join(parser.text).split())
    headings = [' '.join(''.join(parts).split()) for parts in parser.headings]
    headings = [heading for heading in headings if heading]
    label, basis = short_label(page_title, headings, path.name)
    return {'id': hashlib.sha256(str(path).encode()).hexdigest()[:24],
            'path': str(path), 'uri': path.as_uri(), 'filename': path.name,
            'title': title[:240], 'label': label, 'basis': basis,
            'snippet': ' · '.join(headings[:3])[:160] or text[:160],
            'text': text, 'modified': info.st_mtime, 'size': info.st_size,
            'digest': ('partial:' + hashlib.sha256(str(path).encode()).hexdigest()) if truncated
                      else hashlib.sha256(raw).hexdigest(),
            'truncated': truncated or parser.length >= MAX_TEXT, 'encoding': encoding}


def filename_record(path: Path) -> dict:
    """Keep discoverable regular HTML when cloud/offline bytes cannot be read."""
    if not allowed_path(path):
        raise ValueError('不允许的路径')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('不是普通文件')
    identifier = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    title = clean_title(path.stem)
    label, basis = short_label('', [], path.name)
    return {'id': identifier, 'path': str(path), 'uri': path.as_uri(), 'filename': path.name,
            'title': title, 'label': label, 'basis': basis, 'snippet': '正文暂不可读取；支持文件名搜索，可尝试打开原文。',
            'text': '', 'modified': info.st_mtime, 'size': info.st_size,
            'digest': 'unreadable:' + identifier, 'unreadable': True, 'encoding': 'not_read'}


def discover(roots: list[Path], *, max_entries=150_000, max_files=12_000,
             max_seconds=60, exclude: list[Path] | None = None,
             root_entry_limit=50_000) -> tuple[list[dict], dict]:
    start = time.monotonic()
    records: list[dict] = []
    seen: set[str] = set()
    excludes = {str(p.absolute()) for p in (exclude or [])}
    report = {'roots': [], 'visited': 0, 'ignored_dirs': 0, 'issues': [],
              'issue_count': 0, 'limited': False, 'count': 0, 'elapsed': 0, 'text_chars': 0,
              'ignored_paths': [], 'ignored_catalogs': 0, 'truncated_count': 0}

    def issue(path, error):
        report['issue_count'] += 1
        if len(report['issues']) < 30:
            report['issues'].append({'path': str(path), 'error': str(error)[:180]})

    def expired():
        return (report['visited'] >= max_entries or len(records) >= max_files
                or report['text_chars'] >= 40_000_000
                or time.monotonic() - start >= max_seconds)

    for supplied in roots:
        root = supplied.expanduser().absolute()
        item = {'path': str(root), 'status': 'scanned', 'count': 0}
        report['roots'].append(item)
        if not root.is_dir() or not allowed_path(root):
            item['status'] = 'unavailable'
            issue(root, '目录不存在，或为隐藏/敏感/符号链接路径')
            continue
        if expired():
            report['limited'] = True
            item['status'] = 'not_scanned_limit'
            continue
        root_start = report['visited']

        def walk_error(error):
            issue(error.filename, error)
            item['status'] = 'unavailable' if error.filename == str(root) else 'partial_error'

        for base, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            keep = []
            for name in sorted(dirs):
                directory = Path(base) / name
                if (excluded_directory_name(name)
                        or not allowed_path(directory) or str(directory) in excludes):
                    report['ignored_dirs'] += 1
                    if len(report['ignored_paths']) < 30:
                        report['ignored_paths'].append(str(directory))
                else:
                    keep.append(name)
            dirs[:] = keep
            for name in sorted(names):
                report['visited'] += 1
                if expired() or report['visited'] - root_start >= root_entry_limit:
                    report['limited'] = True
                    item['status'] = 'partial_limit'
                    break
                path = Path(base) / name
                if path.suffix.lower() not in {'.html', '.htm'} or not allowed_path(path):
                    continue
                if str(path) in excludes or str(path) in seen:
                    continue
                seen.add(str(path))
                try:
                    record = extract(path)
                    record['root'] = str(root)
                    record['folder'] = str(path.parent.relative_to(root)) or '.'
                    records.append(record)
                    report['text_chars'] += len(record['text'])
                    report['truncated_count'] += int(record['truncated'])
                    item['count'] += 1
                except CatalogExport:
                    report['ignored_catalogs'] += 1
                except (OSError, ValueError) as error:
                    issue(path, error)
                    if item['status'] == 'scanned':
                        item['status'] = 'partial_error'
                    try:
                        record = filename_record(path)
                        record.update(root=str(root), folder=str(path.parent.relative_to(root)) or '.')
                        records.append(record)
                        item['count'] += 1
                    except (OSError, ValueError):
                        pass
            if expired() or report['visited'] - root_start >= root_entry_limit:
                report['limited'] = True
                item['status'] = 'partial_limit'
                break
    records.sort(key=lambda x: x['modified'], reverse=True)
    report.update(count=len(records), elapsed=round(time.monotonic() - start, 2),
                  scanned_at=time.strftime('%Y-%m-%d %H:%M:%S'))
    return records, report
