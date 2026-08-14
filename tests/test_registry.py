"""Tests for the semantic-role control registry and finder (registry.py).

Uses tiny fake pywinauto stand-ins that implement exactly the duck-typed
surface ``ControlFinder`` touches: ``child_window()``/``children()``/``exists()``
on the window/spec and ``is_visible()``/``rectangle()`` on controls.
"""

from __future__ import annotations

from typing import Optional

import pytest

from fakturama_i2c.config import Settings
from fakturama_i2c.ui.registry import ControlFinder, Role, Strategy, matches_exact
from fakturama_i2c.utils.errors import ControlNotFoundError, ManualReviewError


class FakeRect:
    def __init__(self, width: int, height: int) -> None:
        self._w = width
        self._h = height

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


class FakeControl:
    def __init__(
        self,
        *,
        auto_id: Optional[str] = None,
        control_type: Optional[str] = None,
        title: Optional[str] = None,
        class_name: Optional[str] = None,
        visible: bool = True,
        width: int = 10,
        height: int = 10,
    ) -> None:
        self.auto_id = auto_id
        self.control_type = control_type
        self.title = title
        self.class_name = class_name
        self._visible = visible
        self._w = width
        self._h = height

    def is_visible(self) -> bool:
        return self._visible

    def rectangle(self) -> FakeRect:
        return FakeRect(self._w, self._h)

    def exists(self, timeout: float = 0) -> bool:
        return True


def _matches(candidate: object, kwargs: dict) -> bool:
    for key, value in kwargs.items():
        if key == "title_re":
            import re

            title = getattr(candidate, "title", None) or ""
            if not re.search(value, title, re.IGNORECASE):
                return False
        elif getattr(candidate, key, None) != value:
            return False
    return True


class FakeSpec:
    """A pywinauto-style wrapper spec that scopes searches to resolved elements."""

    def __init__(self, scope: list, kwargs: dict) -> None:
        self._scope = scope
        self._kwargs = kwargs

    def _resolve(self) -> list:
        out = []
        for element in self._scope:
            for candidate in element.descendants():
                if _matches(candidate, self._kwargs):
                    out.append(candidate)
        return out

    def descendants(self, **criteria: object) -> list:
        out = []
        for element in self._resolve():
            out.append(element)
            out.extend(element.descendants())
        return [c for c in out if _matches(c, criteria)]

    def exists(self, timeout: float = 0.5) -> bool:
        return bool(self._resolve())

    def child_window(self, **kwargs: object) -> "FakeSpec":
        return FakeSpec(self._resolve(), kwargs)


class FakeControl:
    def __init__(
        self,
        *,
        auto_id: Optional[str] = None,
        control_type: Optional[str] = None,
        title: Optional[str] = None,
        class_name: Optional[str] = None,
        visible: bool = True,
        width: int = 10,
        height: int = 10,
    ) -> None:
        self.auto_id = auto_id
        self.control_type = control_type
        self.title = title
        self.class_name = class_name
        self._visible = visible
        self._w = width
        self._h = height

    def descendants(self) -> list:
        return []

    def is_visible(self) -> bool:
        return self._visible

    def rectangle(self) -> FakeRect:
        return FakeRect(self._w, self._h)

    def exists(self, timeout: float = 0) -> bool:
        return True


class FakeWin:
    def __init__(
        self,
        children: Optional[list] = None,
        *,
        title: Optional[str] = None,
        auto_id: Optional[str] = None,
    ) -> None:
        self.children = list(children or [])
        self.title = title
        self.auto_id = auto_id
        self.control_type = None
        self.class_name = None

    def descendants(self, **criteria: object) -> list:
        out = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        if criteria:
            out = [c for c in out if _matches(c, criteria)]
        return out

    def child_window(self, **kwargs: object) -> FakeSpec:
        return FakeSpec([self], kwargs)


def _win(*controls: object) -> FakeWin:
    return FakeWin(list(controls))


def test_resolve_by_auto_id() -> None:
    save = FakeControl(auto_id="save")
    win = _win(save, FakeControl(auto_id="cancel"))
    registry = {"SAVE": Role("SAVE").add(Strategy("auto_id", "save"))}
    assert ControlFinder(Settings(), registry).resolve(win, "SAVE") is save


def test_resolve_by_control_type() -> None:
    table = FakeControl(control_type="Table")
    win = _win(table, FakeControl(control_type="Edit"))
    registry = {"T": Role("T").add(Strategy("control_type", "Table"))}
    assert ControlFinder(Settings(), registry).resolve(win, "T") is table


def test_resolve_by_class() -> None:
    button = FakeControl(class_name="SWT.Button")
    win = _win(button)
    registry = {"B": Role("B").add(Strategy("class", "SWT.Button"))}
    assert ControlFinder(Settings(), registry).resolve(win, "B") is button


def test_resolve_skips_hidden_controls() -> None:
    hidden = FakeControl(auto_id="save", visible=False)
    visible = FakeControl(auto_id="save")
    win = _win(hidden, visible)
    registry = {"SAVE": Role("SAVE").add(Strategy("auto_id", "save"))}
    assert ControlFinder(Settings(), registry).resolve(win, "SAVE") is visible


def test_multiple_matches_raise_manual_review() -> None:
    win = _win(FakeControl(auto_id="dup"), FakeControl(auto_id="dup"))
    registry = {"DUP": Role("DUP").add(Strategy("auto_id", "dup"))}
    with pytest.raises(ManualReviewError):
        ControlFinder(Settings(), registry).resolve(win, "DUP")


def test_require_unique_false_allows_multiple() -> None:
    a = FakeControl(auto_id="dup")
    b = FakeControl(auto_id="dup")
    win = _win(a, b)
    registry = {"DUP": Role("DUP").add(Strategy("auto_id", "dup", require_unique=False))}
    assert ControlFinder(Settings(), registry).resolve(win, "DUP") is a


def test_no_match_raises_control_not_found() -> None:
    win = _win(FakeControl(auto_id="other"))
    registry = {"SAVE": Role("SAVE").add(Strategy("auto_id", "save"))}
    with pytest.raises(ControlNotFoundError):
        ControlFinder(Settings(), registry).resolve(win, "SAVE")


def test_unknown_role_raises() -> None:
    with pytest.raises(ControlNotFoundError):
        ControlFinder(Settings(), {}).resolve(_win(), "NOPE")


def test_ancestor_scoping_disambiguates() -> None:
    outside = FakeControl(auto_id="contact.new")
    group = FakeWin(
        title="Contact area",
        children=[FakeControl(auto_id="contact.new")],
    )
    win = _win(outside, group)
    registry = {
        "NEW_CONTACT": Role("NEW_CONTACT").add(
            Strategy("auto_id", "contact.new", in_ancestor="Contact area", ancestor_kind="name")
        )
    }
    result = ControlFinder(Settings(), registry).resolve(win, "NEW_CONTACT")
    assert result.auto_id == "contact.new"


def test_matches_exact_case_insensitive_trimmed() -> None:
    assert matches_exact("Acme GmbH", ["acme gmbh", "ACME Inc"]) == ["acme gmbh"]
    assert matches_exact("  Acme GmbH  ", ["ACME GMBH", "Acme Inc"]) == ["ACME GMBH"]


def test_matches_exact_no_partial() -> None:
    assert matches_exact("Acme", ["acme gmbh", "ACME Inc"]) == []


def test_strategy_to_child_window_kwargs() -> None:
    assert Strategy("auto_id", "x").to_child_window_kwargs() == {"auto_id": "x"}
    assert Strategy("control_type", "Edit").to_child_window_kwargs() == {"control_type": "Edit"}
    assert Strategy("name", "Save").to_child_window_kwargs() == {"title": "Save"}
    assert Strategy("name", "Sav.*", regex=True).to_child_window_kwargs() == {"title_re": "Sav.*"}
    assert Strategy("class", "SWT.Button").to_child_window_kwargs() == {"class_name": "SWT.Button"}