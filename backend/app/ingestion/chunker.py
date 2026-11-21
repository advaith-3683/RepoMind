"""Code-aware chunking.

Python uses the stdlib `ast` module (exact function/class boundaries).
JS/TS/Go/Rust use top-level definition regexes (heuristic).
Everything else falls back to overlapping line windows.
Phase-2 upgrade: tree-sitter for all languages.

All line numbers are 1-indexed and inclusive."""
import ast
import re
from dataclasses import dataclass

MAX_CHUNK_LINES = 120
OVERLAP = 10
WINDOW = 60

EXT_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".kt": "kotlin", ".swift": "swift",
    ".md": "markdown", ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".sql": "sql", ".sh": "shell",
}


@dataclass
class Chunk:
    path: str
    language: str
    symbol: str | None
    kind: str
    start_line: int
    end_line: int
    content: str

    def embed_text(self) -> str:
        header = f"# file: {self.path}"
        if self.symbol:
            header += f" | {self.kind}: {self.symbol}"
        return f"{header}\n{self.content}"


def detect_language(path: str) -> str | None:
    dot = path.rfind(".")
    return EXT_LANG.get(path[dot:].lower()) if dot != -1 else None


def _split_large(chunk: Chunk, lines: list[str]) -> list[Chunk]:
    if chunk.end_line - chunk.start_line + 1 <= MAX_CHUNK_LINES:
        return [chunk]
    out, s, part = [], chunk.start_line, 1
    while s <= chunk.end_line:
        e = min(s + MAX_CHUNK_LINES - 1, chunk.end_line)
        sym = f"{chunk.symbol}#part{part}" if chunk.symbol else None
        out.append(Chunk(chunk.path, chunk.language, sym, chunk.kind, s, e, "\n".join(lines[s - 1:e])))
        if e == chunk.end_line:
            break
        s = e - OVERLAP + 1
        part += 1
    return out


def _uncovered(total: int, covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    mark = [False] * (total + 2)
    for s, e in covered:
        for i in range(s, e + 1):
            mark[i] = True
    ranges, i = [], 1
    while i <= total:
        if mark[i]:
            i += 1
            continue
        j = i
        while j + 1 <= total and not mark[j + 1]:
            j += 1
        ranges.append((i, j))
        i = j + 1
    return ranges


def _module_chunks(path, lang, lines, ranges) -> list[Chunk]:
    out = []
    for s, e in ranges:
        while s <= e and not lines[s - 1].strip():
            s += 1
        while e >= s and not lines[e - 1].strip():
            e -= 1
        if sum(1 for x in lines[s - 1:e] if x.strip()) >= 2:
            out.append(Chunk(path, lang, None, "module", s, e, "\n".join(lines[s - 1:e])))
    return out


def chunk_windows(path: str, lang: str, lines: list[str], window: int = WINDOW, overlap: int = OVERLAP) -> list[Chunk]:
    out, n, s = [], len(lines), 1
    while s <= n:
        e = min(s + window - 1, n)
        seg = lines[s - 1:e]
        if any(x.strip() for x in seg):
            out.append(Chunk(path, lang, None, "text", s, e, "\n".join(seg)))
        if e == n:
            break
        s = e - overlap + 1
    return out


def _start_of(node) -> int:
    decs = [d.lineno for d in getattr(node, "decorator_list", [])]
    return min([node.lineno] + decs)


def chunk_python(path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return chunk_windows(path, "python", lines)

    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    chunks: list[Chunk] = []
    covered: list[tuple[int, int]] = []

    def add(symbol, kind, s, e):
        chunks.append(Chunk(path, "python", symbol, kind, s, e, "\n".join(lines[s - 1:e])))

    for node in tree.body:
        if isinstance(node, funcs):
            s, e = _start_of(node), node.end_lineno
            add(node.name, "function", s, e)
            covered.append((s, e))
        elif isinstance(node, ast.ClassDef):
            s, e = _start_of(node), node.end_lineno
            methods = [n for n in node.body if isinstance(n, funcs)]
            if e - s + 1 <= MAX_CHUNK_LINES or not methods:
                add(node.name, "class", s, e)
            else:
                header_end = _start_of(methods[0]) - 1
                if header_end >= s:
                    add(node.name, "class", s, header_end)
                for m in methods:
                    add(f"{node.name}.{m.name}", "method", _start_of(m), m.end_lineno)
            covered.append((s, e))

    chunks += _module_chunks(path, "python", lines, _uncovered(len(lines), covered))
    out: list[Chunk] = []
    for c in sorted(chunks, key=lambda c: c.start_line):
        out.extend(_split_large(c, lines))
    return out


_JS = [
    re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),
    re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"),
    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>"),
]
BOUNDARIES = {
    "javascript": _JS,
    "typescript": _JS,
    "go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"), re.compile(r"^type\s+(\w+)\s+(?:struct|interface)")],
    "rust": [
        re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
        re.compile(r"^(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)"),
    ],
}


def chunk_regex(path: str, lang: str, lines: list[str]) -> list[Chunk]:
    pats = BOUNDARIES.get(lang)
    if not pats:
        return chunk_windows(path, lang, lines)
    marks = []
    for i, line in enumerate(lines, 1):
        for p in pats:
            m = p.match(line)
            if m:
                marks.append((i, m.group(1)))
                break
    if not marks:
        return chunk_windows(path, lang, lines)

    chunks: list[Chunk] = []
    if marks[0][0] > 1:
        chunks += _module_chunks(path, lang, lines, [(1, marks[0][0] - 1)])
    for idx, (start, sym) in enumerate(marks):
        end = marks[idx + 1][0] - 1 if idx + 1 < len(marks) else len(lines)
        while end > start and not lines[end - 1].strip():
            end -= 1
        chunks.append(Chunk(path, lang, sym, "definition", start, end, "\n".join(lines[start - 1:end])))
    out: list[Chunk] = []
    for c in chunks:
        out.extend(_split_large(c, lines))
    return out


def chunk_file(path: str, text: str) -> list[Chunk]:
    lang = detect_language(path)
    if lang is None:
        return []
    if lang == "python":
        return chunk_python(path, text)
    lines = text.splitlines()
    if lang in BOUNDARIES:
        return chunk_regex(path, lang, lines)
    return chunk_windows(path, lang, lines)
