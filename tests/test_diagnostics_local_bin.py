from tldr import diagnostics as diag


def test_resolve_tool_finds_plain_project_local_bin(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "src" / "app.ts"
    source.parent.mkdir()
    source.write_text("const answer = 42;\n")
    local_tool = make_executable(tmp_path / "node_modules" / ".bin" / "oxlint")

    assert diag._resolve_tool("oxlint", source) == str(local_tool)


def test_resolve_tool_walks_to_workspace_root(tmp_path, monkeypatch, make_executable):
    monkeypatch.setattr(diag.shutil, "which", lambda name: None)

    source = tmp_path / "packages" / "web" / "src" / "app.ts"
    source.parent.mkdir(parents=True)
    source.write_text("const answer = 42;\n")
    (tmp_path / "packages" / "web" / "package.json").write_text("{}\n")
    root_tool = make_executable(tmp_path / "node_modules" / ".bin" / "oxlint")

    assert diag._resolve_tool("oxlint", source) == str(root_tool)


def test_resolve_tool_falls_back_to_path(tmp_path, monkeypatch):
    source = tmp_path / "app.ts"
    source.write_text("const answer = 42;\n")
    monkeypatch.setattr(
        diag.shutil,
        "which",
        lambda name: f"/usr/local/bin/{name}",
    )

    assert diag._resolve_tool("oxlint", source) == "/usr/local/bin/oxlint"
