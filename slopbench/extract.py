"""
slopbench — built by Blvkware (https://blvkware.dev)

Extract the package dependencies a model actually reached for.

This module decides what counts as "the model suggested a package." Every
number the benchmark reports rests on it, so it is deliberately conservative:
a false extraction becomes a false hallucination becomes a claim about someone
else's product that does not survive them checking it. When in doubt, drop the
candidate.

Three things are dropped that a naive regex would keep:

1. Standard library. `import asyncio` is not a dependency. The stdlib list
   ships as data and unions across Python versions, so the result does not
   depend on which interpreter ran the benchmark.

2. Import names that differ from distribution names. `import yaml` installs
   `PyYAML`; `import cv2` installs `opencv-python`. Reporting `yaml` as a
   hallucination because PyPI has no `yaml` would be the single fastest way
   to lose a reader.

3. Modules the response defines itself. If a model writes `utils.py` and then
   writes `from utils import helper`, that is coherent code, not an invented
   dependency.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

_DATA = os.path.join(os.path.dirname(__file__), "data")


def _load(name: str):
    with open(os.path.join(_DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


PY_STDLIB: Set[str] = set(_load("py_stdlib.json"))

# Node builtins. `node:`-prefixed specifiers are stripped before lookup.
NODE_BUILTINS: Set[str] = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib", "test",
}

RUST_BUILTINS: Set[str] = {
    "std", "core", "alloc", "crate", "self", "super", "proc_macro", "test",
}

# Import name -> distribution name, for the cases where they diverge. Only
# pairs verified against the real distribution are listed; guessing here
# manufactures false positives in the opposite direction.
PY_IMPORT_TO_DIST: Dict[str, str] = {
    "attr": "attrs", "bs4": "beautifulsoup4", "cv2": "opencv-python",
    "dateutil": "python-dateutil", "dns": "dnspython", "docx": "python-docx",
    "dotenv": "python-dotenv", "fitz": "PyMuPDF", "git": "GitPython",
    "google": "protobuf", "grpc": "grpcio", "jose": "python-jose",
    "jwt": "PyJWT", "magic": "python-magic", "mpl_toolkits": "matplotlib",
    "OpenSSL": "pyOpenSSL", "PIL": "Pillow", "pptx": "python-pptx",
    "psycopg2": "psycopg2-binary", "pkg_resources": "setuptools",
    "serial": "pyserial", "skimage": "scikit-image", "sklearn": "scikit-learn",
    "slugify": "python-slugify", "socks": "PySocks", "sqlalchemy": "SQLAlchemy",
    "usb": "pyusb", "win32api": "pywin32", "win32com": "pywin32",
    "wx": "wxPython", "yaml": "PyYAML", "zoneinfo": "backports.zoneinfo",
}

# Fenced code blocks, with the info string captured so we can tell an install
# snippet from a source file.
_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)

_PY_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)", re.M)
_PY_FROM = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s", re.M)
_PY_FILEDEF = re.compile(r"([A-Za-z_]\w*)\.py\b")

_JS_IMPORT = re.compile(r"""\bfrom\s+['"]([^'"]+)['"]""")
_JS_BARE_IMPORT = re.compile(r"""\bimport\s+['"]([^'"]+)['"]""")
_JS_REQUIRE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_RS_USE = re.compile(r"^\s*use\s+([A-Za-z_][\w]*)\s*(?:::|;)", re.M)
_RS_EXTERN = re.compile(r"^\s*extern\s+crate\s+([A-Za-z_][\w]*)", re.M)
# Cargo.toml dependency lines: `serde = "1.0"` or `serde = { version = ... }`
_RS_CARGO = re.compile(r"^\s*([A-Za-z][\w-]*)\s*=\s*[\"{]", re.M)

_INSTALL = [
    (re.compile(r"\bnpm\s+(?:i|install|add)\s+([^\n&|;]+)", re.I), "npm"),
    (re.compile(r"\b(?:yarn|pnpm|bun)\s+add\s+([^\n&|;]+)", re.I), "npm"),
    (re.compile(r"\bpip3?\s+install\s+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\buv\s+(?:pip\s+)?(?:install|add)\s+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\bpoetry\s+add\s+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\bcargo\s+add\s+([^\n&|;]+)", re.I), "crates"),
]

_PY_SPEC = re.compile(r"[<>=!~\[;].*$")
_VERSION_ONLY = re.compile(r"^[\d.]+$")


@dataclass(frozen=True)
class Suggestion:
    """One package a model reached for, and how it reached for it."""
    name: str
    ecosystem: str
    source: str  # "import" | "install"

    def key(self) -> str:
        return f"{self.ecosystem}:{self.name.lower()}"


def _code_blocks(text: str) -> List[tuple]:
    return [(m.group(1).strip().lower(), m.group(2)) for m in _FENCE.finditer(text)]


def _locally_defined(text: str) -> Set[str]:
    """Module names the response defines itself.

    Picks up `# utils.py` headers, `**app/models.py**` captions, and fenced
    blocks whose info string names a file. These are the model's own files;
    importing them is not a dependency claim.
    """
    local = {n.lower() for n in _PY_FILEDEF.findall(text)}
    for info, _ in _code_blocks(text):
        for part in re.split(r"[\s:=]+", info):
            if part.endswith(".py"):
                local.add(part[:-3].rsplit("/", 1)[-1].lower())
    return local


def _clean_py(token: str) -> Optional[str]:
    token = _PY_SPEC.sub("", token.strip().strip("\"'`,")).strip()
    if not token or token.startswith("-") or _VERSION_ONLY.match(token):
        return None
    if token.startswith(".") or "://" in token or "/" in token:
        return None
    return token


def _clean_js(token: str) -> Optional[str]:
    token = token.strip().strip("\"'`,")
    if not token or token.startswith("-") or "://" in token:
        return None
    if token.startswith(".") or token.startswith("/"):
        return None
    if token.startswith("@"):
        parts = token.split("/")
        if len(parts) < 2:
            return None
        scope_pkg = "/".join(parts[:2])
        return scope_pkg.split("@", 2)[0] + "@" + scope_pkg.split("@", 2)[1] \
            if scope_pkg.count("@") > 1 else scope_pkg
    base = token.split("/")[0]
    base = base.split("@")[0] if not base.startswith("@") else base
    return base or None


def _specifier_to_package(spec: str) -> Optional[str]:
    """Turn a JS module specifier into the package that provides it."""
    cleaned = _clean_js(spec)
    if cleaned is None:
        return None
    root = cleaned[5:] if cleaned.startswith("node:") else cleaned
    if root in NODE_BUILTINS:
        return None
    return root


def extract(text: str, ecosystem: Optional[str] = None) -> List[Suggestion]:
    """Extract package suggestions from a model response.

    `ecosystem` scopes import parsing to the language the prompt asked for.
    Install commands are always parsed, because they name their own ecosystem
    and a model answering a Python prompt with `npm install` is a finding in
    its own right.
    """
    found: Dict[str, Suggestion] = {}

    def add(name: Optional[str], eco: str, source: str) -> None:
        if not name:
            return
        s = Suggestion(name=name, ecosystem=eco, source=source)
        found.setdefault(s.key(), s)

    for pattern, eco in _INSTALL:
        for match in pattern.finditer(text):
            for token in match.group(1).split():
                if eco == "npm":
                    add(_specifier_to_package(token), eco, "install")
                else:
                    cleaned = _clean_py(token)
                    if cleaned:
                        add(cleaned, eco, "install")

    blocks = _code_blocks(text)
    code = "\n".join(body for _, body in blocks) if blocks else text
    local = _locally_defined(text)

    if ecosystem in (None, "pypi"):
        roots: Set[str] = set()
        for match in _PY_IMPORT.finditer(code):
            for part in match.group(1).split(","):
                roots.add(part.strip().split(".")[0])
        for match in _PY_FROM.finditer(code):
            roots.add(match.group(1).split(".")[0])
        for root in roots:
            if not root or root in PY_STDLIB or root.lower() in local:
                continue
            add(PY_IMPORT_TO_DIST.get(root, root), "pypi", "import")

    if ecosystem in (None, "npm"):
        for pattern in (_JS_IMPORT, _JS_BARE_IMPORT, _JS_REQUIRE):
            for match in pattern.finditer(code):
                add(_specifier_to_package(match.group(1)), "npm", "import")

    if ecosystem in (None, "crates"):
        for pattern in (_RS_USE, _RS_EXTERN):
            for match in pattern.finditer(code):
                name = match.group(1)
                if name not in RUST_BUILTINS:
                    add(name.replace("_", "-"), "crates", "import")
        for info, body in blocks:
            if "toml" in info or "[dependencies]" in body:
                section = body.split("[dependencies]", 1)[-1]
                section = re.split(r"^\[", section, maxsplit=1, flags=re.M)[0]
                for match in _RS_CARGO.finditer(section):
                    add(match.group(1), "crates", "install")

    return sorted(found.values(), key=lambda s: (s.ecosystem, s.name.lower()))


def extract_all(texts: Iterable[str], ecosystem: Optional[str] = None) -> List[List[Suggestion]]:
    return [extract(t, ecosystem) for t in texts]
