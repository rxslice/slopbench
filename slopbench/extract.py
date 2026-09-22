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

4. Prose that happens to follow an install command. Install commands are read
   only from fenced blocks, inline code spans, and lines that begin with a
   package manager, and the package list stops at the first token that could
   not be a name on that registry. Reading them out of running text turned
   "run pip install pandas to load the data" into four PyPI packages, three of
   which were English words that recur in every sample and would have scored as
   targetable. The cost is that a package named only in prose is missed; that
   is the right direction to be wrong in.
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

# Whitespace inside a command is `[ \t]`, never `\s`. With `\s`, a bare
# `npm install` (install what package.json lists, no names) matched across the
# line break and read the next line as its package list: the code block after
# `npm init -y && npm install` was `node script.js input.ndjson output.ndjson`,
# and all three file names came out as invented npm packages. They recur in
# every sample of that prompt, so they would have been reported as targetable.
_INSTALL = [
    (re.compile(r"\bnpm[ \t]+(?:i|install|add)[ \t]+([^\n&|;]+)", re.I), "npm"),
    (re.compile(r"\b(?:yarn|pnpm|bun)[ \t]+add[ \t]+([^\n&|;]+)", re.I), "npm"),
    (re.compile(r"\bpip3?[ \t]+install[ \t]+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\buv[ \t]+(?:pip[ \t]+)?(?:install|add)[ \t]+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\bpoetry[ \t]+add[ \t]+([^\n&|;]+)", re.I), "pypi"),
    (re.compile(r"\bcargo[ \t]+add[ \t]+([^\n&|;]+)", re.I), "crates"),
]

# Install commands are read only off the *command surface* of a response:
# fenced blocks, inline code spans, and lines that begin with a package manager.
# Scanning the whole response was the largest single source of invented
# findings. "To get started, run pip install pandas to load the data." yielded
# `to`, `load`, `the` and `data.` as PyPI packages — every one of them 404s,
# every one recurs across samples because it is English, and every one would
# have been reported as a targetable hallucination in someone else's product.
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_COMMAND_LINE = re.compile(
    r"^[ \t]*(?:[$>#][ \t]+)?(?:sudo[ \t]+)?(?:python\d?[ \t]+-m[ \t]+)?"
    r"(?:npm|yarn|pnpm|bun|pip3?|uv|poetry|cargo)\b.*$",
    re.M | re.I,
)

# Flags whose argument is a path, a URL, or a feature list, never a package.
# `pip install -r requirements.txt` reporting `requirements.txt` as an invented
# PyPI package is the canonical version of this mistake.
_FLAGS_TAKING_A_VALUE = {
    # pip / uv / poetry
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix",
    "--root", "--src", "--log", "--cache-dir", "--proxy", "--cert",
    "--client-cert", "--python", "--platform", "--abi", "--implementation",
    "-e", "--editable",
    # cargo
    "--registry", "--features", "--manifest-path", "--path", "--git",
    "--branch", "--tag", "--rev", "--package", "-p", "--target-dir",
    # npm / yarn / pnpm / bun
    "-w", "--filter", "--workspace",
}

# Backstop behind the flag table: nobody publishes `requirements.txt`.
_FILE_SUFFIXES = (
    ".txt", ".toml", ".lock", ".cfg", ".ini", ".in", ".yml", ".yaml", ".json",
    ".py", ".whl", ".zip", ".md", ".tar.gz",
    # Data files a script is pointed at. `.js` is deliberately absent:
    # chart.js and highlight.js are real packages.
    ".ndjson", ".jsonl", ".csv", ".tsv", ".sh",
)

# A token that could not be a package name on the registry it is claimed for is
# a parsing mistake, not a finding. Requiring both ends to be alphanumeric is
# what rejects prose trailing off the end of a command: `axios.`, `data.`.
_NAME_GRAMMAR = {
    "npm": re.compile(
        r"^(?:@[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/)?"
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
    ),
    "pypi": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"),
    "crates": re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$"),
}
MAX_NAME_LENGTH = 214  # npm's limit, comfortably above the other two

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


def _command_surface(text: str, blocks: List[tuple]) -> str:
    """The parts of a response where a shell command can legitimately appear."""
    parts = [body for _info, body in blocks]
    parts.extend(_INLINE_CODE.findall(text))
    parts.extend(_COMMAND_LINE.findall(text))
    return "\n".join(parts)


def _plausible(name: Optional[str], ecosystem: str) -> bool:
    if not name or len(name) > MAX_NAME_LENGTH:
        return False
    if name.lower().endswith(_FILE_SUFFIXES):
        return False
    grammar = _NAME_GRAMMAR.get(ecosystem)
    return bool(grammar and grammar.match(name))


def _packages_from_install(argv: str, ecosystem: str) -> List[str]:
    """Read the package list off one install command.

    Scanning stops at the first token that is neither a flag nor a possible
    package name. A shell command's package list is contiguous, so a token that
    cannot be a package marks where the command ended and prose began. The rule
    costs nothing on a well-formed command, and it is what keeps a sentence
    from being reported as somebody's invented dependency.
    """
    names: List[str] = []
    tokens = argv.split()
    index = 0
    while index < len(tokens):
        token = tokens[index].strip().strip("\"'`,")
        index += 1
        if not token:
            continue
        if token.startswith("-"):
            if token in _FLAGS_TAKING_A_VALUE:
                index += 1  # its argument is a path, a URL or a feature list
            continue
        cleaned = (
            _specifier_to_package(token) if ecosystem == "npm" else _clean_py(token)
        )
        if cleaned is None:
            # Dropped as a builtin, a relative path or a URL. All of those are
            # legitimate parts of an install command, so keep reading.
            continue
        if not _plausible(cleaned, ecosystem):
            break
        names.append(cleaned)
    return names


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

    blocks = _code_blocks(text)
    surface = _command_surface(text, blocks)

    for pattern, eco in _INSTALL:
        for match in pattern.finditer(surface):
            for name in _packages_from_install(match.group(1), eco):
                add(name, eco, "install")

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
