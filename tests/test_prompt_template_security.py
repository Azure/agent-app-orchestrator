"""Prompt rendering escapes substituted markup and retains strict variables."""

from types import SimpleNamespace

import pytest
from jinja2 import TemplateError

from strategies.base_agent_strategy import BaseAgentStrategy
from strategies import single_agent_rag_strategy_v2


async def test_context_markup_is_escaped_without_escaping_template_text(tmp_path):
    (tmp_path / "main.jinja2").write_text(
        "<context>{{ value }}</context>", encoding="utf-8"
    )
    strategy = SimpleNamespace(_prompt_dir=lambda: str(tmp_path))
    result = await BaseAgentStrategy._read_prompt_jinja2(
        strategy, "main", {"value": '<script>"x" & \'y\'</script>'}
    )
    assert result == (
        "<context>&lt;script&gt;&#34;x&#34; &amp; &#39;y&#39;&lt;/script&gt;</context>"
    )
    with pytest.raises(TemplateError, match="'value' is undefined"):
        await BaseAgentStrategy._read_prompt_jinja2(strategy, "main", {})


def test_reusable_agent_instructions_escape_substituted_values(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompts" / "single_agent_rag"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "main.jinja2").write_text(
        '<context>{{ "<untrusted> & value" }}</context>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        single_agent_rag_strategy_v2, "__file__",
        str(tmp_path / "strategies" / "single_agent_rag_strategy_v2.py"),
    )
    result = single_agent_rag_strategy_v2._render_agent_instructions(
        None, aisearch_enabled=True
    )
    assert result == "<context>&lt;untrusted&gt; &amp; value</context>"
