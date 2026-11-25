"""Accessibility-tree model: one immutable `Snapshot` of everything on screen.

MacroFactor is a Flutter app. Its tree has no resource ids, labels live in `content-desc`
(lines joined by newlines), and panels that are visually covered often stay in the tree.
So every lookup here is structural (parent/child, bounds) rather than "first label match".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from . import PACKAGE

KEYBOARD_PACKAGES = ("com.google.android.inputmethod.latin",)
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))


@dataclass(eq=False)
class Node:
    cls: str
    label: str  # content-desc, else text; zero-width characters removed
    text: str  # value of editable fields
    pkg: str
    x1: int
    y1: int
    x2: int
    y2: int
    clickable: bool = False
    focused: bool = False
    selected: bool = False
    checked: bool = False
    scrollable: bool = False
    hint: str = ""
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list, repr=False)

    @property
    def lines(self) -> list[str]:
        return [s.strip() for s in self.label.split("\n")]

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            yield from child.walk()

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def __str__(self) -> str:
        flags = "".join(c for c, on in (("C", self.clickable), ("F", self.focused), ("S", self.selected), ("K", self.checked)) if on)
        label = self.label.replace("\n", " / ")
        value = f" ={self.text!r}" if self.text and self.text != self.label else ""
        return f"{self.cls} {label!r}{value} [{self.x1},{self.y1} {self.x2},{self.y2}] {flags}".rstrip()


def _clean(value: str | None) -> str:
    return (value or "").translate(_ZERO_WIDTH).strip()


def _build(element: ET.Element, parent: Node | None) -> Node | None:
    match = _BOUNDS.match(element.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    text, desc = _clean(element.get("text")), _clean(element.get("content-desc"))
    node = Node(
        cls=element.get("class", "").rsplit(".", 1)[-1],
        label=desc or text,
        text=text,
        pkg=element.get("package", ""),
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        clickable=element.get("clickable") == "true",
        focused=element.get("focused") == "true",
        selected=element.get("selected") == "true",
        checked=element.get("checked") == "true",
        scrollable=element.get("scrollable") == "true",
        hint=_clean(element.get("hint")),
        parent=parent,
    )
    for child in element:
        built = _build(child, node)
        if built is not None:
            node.children.append(built)
    return node


class Snapshot:
    """All windows on screen at one instant. `app` queries are limited to MacroFactor's window."""

    def __init__(self, roots: list[Node], xml: str = ""):
        self.roots = roots
        self.xml = xml  # raw dump, kept so a failure can be replayed offline
        self.nodes: list[Node] = [n for root in roots for n in root.walk()]

    @classmethod
    def from_xml(cls, xml: str) -> Snapshot:
        hierarchy = ET.fromstring(xml)
        roots = [node for element in hierarchy if (node := _build(element, None)) is not None]
        return cls(roots, xml)

    # ---- windows

    @property
    def foreground(self) -> str:
        """Package the user is looking at: any dialog or other app wins over MacroFactor's window."""
        others = [r.pkg for r in self.roots if r.pkg not in KEYBOARD_PACKAGES and r.pkg not in (PACKAGE, "com.android.systemui")]
        if others:
            return others[0]
        return PACKAGE if any(r.pkg == PACKAGE for r in self.roots) else ""

    @property
    def keyboard_top(self) -> int | None:
        """Y coordinate where the soft keyboard starts, or None when hidden."""
        tops = [n.y1 for n in self.nodes if n.pkg in KEYBOARD_PACKAGES and n.clickable and n.height > 0]
        return min(tops) if tops else None

    # ---- queries on the MacroFactor window

    def app(self, pkg: str = PACKAGE) -> list[Node]:
        return [n for n in self.nodes if n.pkg == pkg]

    def all(self, pred: Callable[[Node], bool], pkg: str = PACKAGE) -> list[Node]:
        return [n for n in self.nodes if n.pkg == pkg and pred(n)]

    def first(self, pred: Callable[[Node], bool], pkg: str = PACKAGE) -> Node | None:
        return next((n for n in self.nodes if n.pkg == pkg and pred(n)), None)

    def labelled(self, label: str, *, clickable: bool | None = None) -> list[Node]:
        return self.all(lambda n: n.label == label and (clickable is None or n.clickable == clickable))

    def has(self, label: str) -> bool:
        return bool(self.labelled(label))

    def dump(self, pkg: str | None = PACKAGE) -> str:
        """Compact indented listing, for humans, agents and failure reports."""
        out: list[str] = []

        def visit(node: Node, depth: int) -> None:
            if pkg is None or node.pkg == pkg:
                interesting = node.label or node.text or node.clickable or node.scrollable or node.cls == "ImageView"
                if interesting:
                    out.append("  " * min(depth, 12) + str(node))
            for child in node.children:
                visit(child, depth + 1)

        for root in self.roots:
            visit(root, 0)
        return "\n".join(out)
