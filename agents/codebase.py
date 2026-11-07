"""Read-only codebase context, so agents write code that fits an existing repository."""

from __future__ import annotations

import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".terraform",
             "runs", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage", ".next"}
TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".tf", ".tfvars", ".md", ".yml", ".yaml", ".json", ".toml",
            ".cfg", ".ini", ".sql", ".sh", ".txt", ".html", ".css", ".go", ".java", ".rb", ".hcl"}
ALWAYS = {"readme.md", "claude.md", "pyproject.toml", "requirements.txt", "package.json", "main.tf"}
SECRET_NAMES = re.compile(r"(^|/)(\.env[^/]*|.*secret.*|.*credential.*|.*\.pem|.*\.key|terraform\.tfstate.*)$", re.I)
STOP = {"the", "and", "for", "with", "add", "fix", "new", "our", "from", "into", "that", "this", "make", "use"}
MAX_FILE_BYTES = 200_000


def list_files(root: Path) -> list[str]:
    root = root.resolve()
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_EXT and p.name.lower() not in ALWAYS:
            continue
        if SECRET_NAMES.search(rel.as_posix()) or p.stat().st_size > MAX_FILE_BYTES:
            continue
        files.append(rel.as_posix())
    return files


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


def build_context(root: Path, query: str, max_chars: int = 40_000, max_files: int = 12,
                  per_file: int = 8_000) -> tuple[str, str]:
    """Return (file_tree, context). The context holds the files most related to the query."""
    root = root.resolve()
    files = list_files(root)
    tree = "\n".join(files[:400]) + (f"\n...and {len(files) - 400} more files" if len(files) > 400 else "")
    words = _words(query)

    scored = []
    for rel in files:
        path_words = _words(rel.replace("/", " ").replace("_", " ").replace("-", " ").replace(".", " "))
        score = 3 * len(words & path_words)
        if Path(rel).name.lower() in ALWAYS and "/" not in rel:
            score += 4
        if score or words:
            try:
                text = (root / rel).read_text(errors="replace")
            except OSError:
                continue
            score += len(words & _words(text[:20_000]))
            if score:
                scored.append((score, rel, text))
    scored.sort(key=lambda s: (-s[0], s[1]))

    blocks, used = [], 0
    for _, rel, text in scored[:max_files]:
        body = text[:per_file] + ("\n...(trimmed)" if len(text) > per_file else "")
        block = f"\n--- {rel} ---\n{body}\n"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)

    context = f"File tree ({len(files)} files):\n{tree}\n\nMost relevant files:{''.join(blocks) or ' (none matched)'}"
    return tree, context
