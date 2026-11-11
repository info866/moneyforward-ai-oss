"""Cloud starter packaging is independent and has no setup side effects."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_starter", ROOT / "scripts/bootstrap_company.py")
assert spec is not None and spec.loader is not None
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def test_generated_company_has_separate_cloud_plugin_and_read_only_gateway(tmp_path):
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    delivery.bootstrap("alpha", first)
    delivery.bootstrap("beta", second)
    for slug, location in (("alpha", first), ("beta", second)):
        manifest = json.loads((location / "integrations/company-operations/plugin.json").read_text())
        codex = json.loads((location / "integrations/company-operations/.codex-plugin/plugin.json").read_text())
        assert manifest["name"] == f"mf-{slug}-operations"
        assert codex["name"] == manifest["name"]
        assert (location / "mcp_servers/company_accounting_server.py").is_file()
        assert "mf-" + slug in (location / "integrations/company-operations/skills/router/SKILL.md").read_text()
        assert (location / "config/offices" / f"{slug}.json").is_file()
        assert not (location / "data").exists()


def test_cloud_mcp_has_no_provider_write_tools_in_source():
    import ast
    code = (ROOT / "mcp_servers/company_accounting_server.py").read_text()
    root = ast.parse(code)
    names = {node.name for node in root.body if isinstance(node, ast.FunctionDef)}
    assert {"get_office", "workflow_preflight", "run_status", "prepare_run_scope"}.issubset(names)
    assert not {"create_journal", "final_google_chat_send", "delete_journal"} & names
    # Local run metadata is opt-in; this never adds a provider write operation.
    assert {"run_begin", "run_resume"}.issubset(names)
    assert "MF_CLOUD_LOCAL_WORKFLOW_ENABLED" in code
    assert "MF_CLOUD_ACCOUNTING_PROFILE" in code


def test_second_generation_replaces_old_router_slug(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    delivery.bootstrap("alpha", first)
    delivery.bootstrap("bravo", second, source=first)
    router = (second / "integrations/company-operations/skills/router/SKILL.md").read_text()
    assert "mf-bravo-accounting-router" in router
    assert "company slug `bravo`" in router
    assert "company slug `alpha`" not in router
    assert "mf-alpha-accounting-router" not in router
    manifest = json.loads((second / "integrations/company-operations/plugin.json").read_text())
    assert manifest["name"] == "mf-bravo-operations"
