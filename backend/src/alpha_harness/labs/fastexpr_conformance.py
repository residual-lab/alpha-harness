"""This grammar and the browser's, read side by side: :mod:`.fastexpr` and ``crates/fastexpr``
as the frontend ships it must give the same tree, the same message and the same text written
back, for every expression of a corpus.

    uv run python -m alpha_harness.labs.fastexpr_conformance [catalog.duckdb]

The corpus is the template gallery and presets, expressions generated, flipped, crossed,
respaced and broken at random, seeded so a failure repeats, and, given a copy of a catalog,
every distinct Alpha in it. Needs ``wasmtime``, a dev dependency.
"""

import json
import random
import re
import struct
import sys
from pathlib import Path
from typing import Any

from wasmtime import Instance, Module, Store

from . import fastexpr as fx
from .template import PRESETS

ROOT = Path(__file__).resolve().parents[4]
WASM = ROOT / "frontend" / "src" / "lib" / "fastexpr.wasm"
GALLERY = ROOT / "frontend" / "src" / "screens" / "research-labs" / "template" / "gallery.tsx"


class Core:
    """The WebAssembly module, called the way the frontend calls it."""

    def __init__(self, path: Path = WASM) -> None:
        self.store = Store()
        exports = Instance(self.store, Module.from_file(self.store.engine, str(path)), []).exports(
            self.store
        )
        self.memory: Any = exports["memory"]
        self.functions: dict[str, Any] = {
            name: exports[name] for name in ("allocate", "release", "parse", "render")
        }
        # One input buffer, grown as needed: each call from Python costs tens of microseconds.
        self.buffer, self.capacity = 0, 0

    def call(self, name: str, text: str, flag: int) -> dict[str, Any]:
        store, data, fn = self.store, text.encode(), self.functions
        if len(data) > self.capacity:
            if self.capacity:
                fn["release"](store, self.buffer, self.capacity)
            self.capacity = max(len(data), 1 << 16)
            self.buffer = fn["allocate"](store, self.capacity)
        self.memory.write(store, data, self.buffer)
        out = fn[name](store, self.buffer, len(data), flag)
        (size,) = struct.unpack("<I", self.memory.read(store, out, out + 4))
        body = bytes(self.memory.read(store, out + 4, out + 4 + size))
        fn["release"](store, out, size + 4)
        return json.loads(body)


def _shape(node: fx.Node) -> dict[str, Any]:
    return {
        "kind": node.kind,
        "value": node.value,
        "args": [_shape(a) for a in node.args],
        "kwargs": [{"name": k, "value": _shape(v)} for k, v in node.kwargs],
    }


def _unplaced(tree: dict[str, Any]) -> dict[str, Any]:
    """The core's tree without the positions this grammar does not keep."""
    return {
        "kind": tree["kind"],
        "value": tree["value"],
        "args": [_unplaced(a) for a in tree["args"]],
        "kwargs": [{"name": k["name"], "value": _unplaced(k["value"])} for k in tree["kwargs"]],
    }


def read_here(text: str) -> tuple[Any, str | None]:
    try:
        tree = fx.parse(text)
    except fx.ParseError as exc:
        return str(exc), None
    return _shape(tree), fx.render(tree)


def read_there(core: Core, text: str) -> tuple[Any, str | None]:
    parsed = core.call("parse", text, 0)
    if "error" in parsed:
        return parsed["error"], None
    return _unplaced(parsed["tree"]), core.call("render", text, 0)["text"]


# -- the corpus -------------------------------------------------------------------------------

_NAMES = ["close", "volume", "cap", "x", "returns", "$f", "$lookback", "...", "nan", "sector"]
_NUMBERS = ["0", "1", "2", "20", "0.5", ".5", "1.", "1e5", "2.5E-3", "3e+2"]
_STRINGS = ['"0, 1, 0.1"', "'x'", '"a # b"', '"a // b /* c */"', '""', '"it\'s"']
_BINARY = ["||", "&&", "==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "^"]
_CALLS = ["rank", "ts_mean", "if_else", "group_rank", "$op", "bucket"]
#: A sign against a power, every way round: BRAIN reads ``-x ^ y`` as ``(-x) ^ y``.
PRECEDENCE = [
    "-x ^ y",
    "-(x ^ y)",
    "(-x) ^ y",
    "-2 ^ 2",
    "2 ^ -2",
    "-2 ^ -2",
    "-close ^ 0.5",
    "-(close ^ 0.5)",
    "x ^ y ^ z",
    "(x ^ y) ^ z",
    "!x ^ 2",
    "-!x ^ 2",
    "--x ^ 2",
    "a - -b ^ c",
    "-(a ? b : c) ^ 2",
]
_BROKEN = [
    *("", " ", ";", "; ;", "/*", "/* x", "*/", "//", "#", '"', "'", '"abc', "$", "$1", ".."),
    *(
        "....",
        "1e",
        "1e+",
        "1.e5",
        "x\u00e9",
        "\u00e9",
        "\u3000x",
        "x\x1c+\x1f1",
        "a == = b",
        "f(,)",
        "f(x,)",
    ),
    *("f(k=)", "a ? b", "a ? b :", "(", ")", "x)", "a = ", "= 1", "a = b = c", "f(a = b = c)"),
    *("rank(x) /* open", "x # c\n+ 1", "x /* a\nb */ + 1", '"a/*b" + "*/c"', "it's # don't"),
    *("x\r\n+\r\n1", "x\u00a0+\u30001", "x\u200b", "x\U0001f600", "x\x00", "x\x85+1"),
]
_TYPED = [*"()[]{},;=+-*/^<>!?:\"'#@$._ 0aZ\n\t%&|\\", "\u00e9", "\U0001f600", "\u200b", "\x1c"]


def _deep() -> list[str]:
    edge = fx.MAX_DEPTH
    return [
        *(f"{'rank(' * n}x{')' * n}" for n in (edge - 1, edge, edge * 2)),
        *(" + ".join(["x"] * n) for n in (edge, edge + 1, edge * 10)),
        *(f"{'-' * n}x" for n in (edge - 1, edge, edge * 30)),
        f"{'(' * edge}x{')' * edge}",
        "x ^ " * edge + "2",
        "a ? " * edge + "1" + " : 2" * edge,
    ]


def gallery() -> list[str]:
    """The built-in templates as the frontend writes them, and every preset as a variable's
    values are read: one call's inputs."""
    source = GALLERY.read_text(encoding="utf-8")
    block = source[source.index("export const GALLERY") :]
    block = block[: block.index("\n]\n")]
    quoted = re.findall(r"'((?:[^'\\\n]|\\.)*)'", block)
    texts = [q.replace("\\n", "\n").replace("\\'", "'") for q in quoted if "(" in q]
    return texts + [f"values({v})" for v in PRESETS.values()]


class Corpus:
    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        #: Generated trees this grammar does not read back from what it wrote.
        self.unread: list[str] = []

    def tree(self, depth: int) -> fx.Node:
        rng = self.rng
        if depth <= 0 or rng.random() < 0.25:
            pick = rng.random()
            if pick < 0.35:
                return fx.Node("num", rng.choice(_NUMBERS))
            return fx.Node(
                *(("str", rng.choice(_STRINGS)) if pick < 0.42 else ("name", rng.choice(_NAMES)))
            )
        kind = rng.random()
        if kind < 0.3:
            args = tuple(self.tree(depth - 1) for _ in range(rng.randint(0, 3)))
            keys = rng.sample(["k", "lag", "range", "driver"], rng.choice([0, 0, 0, 1, 2]))
            kwargs = tuple((key, self.tree(depth - 1)) for key in keys)
            return fx.Node("call", rng.choice(_CALLS), args, kwargs)
        if kind < 0.45:
            return fx.Node("unary", rng.choice(["-", "-", "!"]), (self.tree(depth - 1),))
        if kind < 0.9:
            sides = (self.tree(depth - 1), self.tree(depth - 1))
            return fx.Node("binary", rng.choice(_BINARY), sides)
        return fx.Node("ternary", "", tuple(self.tree(depth - 1) for _ in range(3)))

    def program(self) -> fx.Node:
        steps = tuple(
            fx.Node("assign", self.rng.choice(["a", "step1", "$x"]), (self.tree(4),))
            for _ in range(self.rng.choice([0, 0, 1, 2]))
        )
        last = self.tree(5)
        return fx.Node("seq", args=(*steps, last)) if steps else last

    def respaced(self, text: str) -> str:
        """The same expression with space, line breaks and comments between its tokens."""
        gaps = [" ", "", "\n", "\t", " /* c */ ", " // note\n", " # x\n", "\u00a0"]
        return "".join(t + self.rng.choice(gaps) for _, t, _ in fx.tokenize(text))

    def broken(self, text: str) -> str:
        rng = self.rng
        for _ in range(rng.randint(1, 3)):
            at = rng.randrange(len(text) + 1)
            edit = rng.random()
            if edit < 0.4:
                text = text[:at] + text[at + 1 :]
            elif edit < 0.8:
                text = text[:at] + rng.choice(_TYPED) + text[at:]
            elif edit < 0.9:
                text = text[:at]
            else:
                text = text[:at] + text[at : at + rng.randint(1, 8)] + text[at:]
        return text

    def build(self, alphas: list[str], size: int) -> dict[str, list[str]]:
        trees = [self.program() for _ in range(size)]
        generated = [fx.render(t) for t in trees]
        self.unread = [t for t, tree in zip(generated, trees, strict=True) if fx.parse(t) != tree]
        sources = alphas + generated
        parents = [fx.parse(t) for t in sources]
        flipped = [
            fx.render(fx.negated(t))
            for t in parents[: size * 2] + [fx.parse(t) for t in PRECEDENCE]
            if not (t.kind == "assign" or (t.kind == "seq" and t.args[-1].kind == "assign"))
        ]
        crossed = [fx.crossover(self.rng, *self.rng.sample(parents, 2)) for _ in range(size)]
        return {
            "alphas": alphas,
            "gallery": gallery(),
            "precedence": PRECEDENCE,
            "generated": generated,
            "flipped": flipped,
            "crossed": [fx.render(child) for child in crossed if child is not None],
            "respaced": [self.respaced(t) for t in self.rng.sample(sources, size // 2)],
            "broken": [
                *_BROKEN,
                *_deep(),
                *(self.broken(t) for t in self.rng.sample(sources, size)),
            ],
        }


def vault(path: str) -> list[str]:
    """Every distinct expression in a catalog: a copy, since the app holds its own open."""
    import duckdb

    with duckdb.connect(path, read_only=True) as con:
        rows = con.execute("SELECT DISTINCT expression FROM alpha WHERE expression IS NOT NULL")
        return [str(r[0]) for r in rows.fetchall()]


def main() -> None:
    alphas = vault(sys.argv[1]) if len(sys.argv) > 1 else []
    corpus_maker = Corpus()
    corpus = corpus_maker.build(alphas, size=1500)
    core = Core()
    failed = [f"written back differently: {t[:80]!r}" for t in corpus_maker.unread]
    for name, texts in corpus.items():
        found = [t for t in texts if read_here(t) != read_there(core, t)]
        failed.extend(
            f"{name}: {t[:80]!r} {read_here(t)[0]!r:.80} {read_there(core, t)[0]!r:.80}"
            for t in found[:3]
        )
        sys.stdout.write(f"{name:10} {len(texts):6} read alike {len(texts) - len(found):6}\n")
    if failed:
        raise SystemExit("fastexpr conformance failed:\n" + "\n".join(failed))


if __name__ == "__main__":
    main()
