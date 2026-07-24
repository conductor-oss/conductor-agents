from __future__ import annotations

from tui.chat.tools import _apply_model_choice


def test_explicit_model_maps_to_coding_roles():
    selected = _apply_model_choice("code_parallel", {"model": "gpt-5.6-terra"})
    assert selected["codeModel"] == "gpt-5.6-terra"
    assert selected["codeAgent"] == "codex"
