import json
import subprocess

from code_briefcase.context_pack import build_context_pack


def test_explicit_file_produces_function_outline(tmp_path):
    source = tmp_path / "auth.py"
    source.write_text("def login(username: str) -> bool:\n    return True\n")

    pack = build_context_pack("fix login bug", project=tmp_path, files=["auth.py"], budget=1000)

    assert pack.items[0].path == "auth.py"
    assert "login" in pack.to_markdown()


def test_budget_is_enforced(tmp_path):
    source = tmp_path / "big.py"
    source.write_text("\n".join(f"def f{i}():\n    return {i}" for i in range(200)))

    pack = build_context_pack("big", project=tmp_path, files=["big.py"], budget=100)

    assert pack.estimated_tokens <= 120


def test_missing_semantic_index_does_not_crash(tmp_path):
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")

    pack = build_context_pack("main", project=tmp_path, include_semantic=True)

    assert pack.items


def test_changed_mode_handles_no_git_repo_gracefully(tmp_path):
    (tmp_path / "app.py").write_text("def main():\n    return 1\n")

    pack = build_context_pack("", project=tmp_path, changed=True)

    json.dumps(pack.to_dict())


def test_secret_looking_files_are_excluded(tmp_path):
    (tmp_path / ".env.py").write_text("def secret():\n    return 'x'\n")

    pack = build_context_pack("secret", project=tmp_path, files=[".env.py"])

    assert all(item.path != ".env.py" for item in pack.items)


def test_tldrignore_excludes_explicit_file_and_fallback_tree(tmp_path):
    (tmp_path / ".code-briefcaseignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text("def ignored_target():\n    return True\n")

    pack = build_context_pack("ignored_target", project=tmp_path, files=["ignored.py"])

    assert "ignored.py" not in json.dumps(pack.to_dict())


def test_tldrignore_excludes_text_search_matches(tmp_path):
    (tmp_path / ".code-briefcaseignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text("def ignored_target():\n    return True\n")
    (tmp_path / "visible.py").write_text("def visible_target():\n    return True\n")

    pack = build_context_pack("target", project=tmp_path, include_semantic=False)

    assert any(item.path == "visible.py" for item in pack.items)
    assert all(item.path != "ignored.py" for item in pack.items)


def test_changed_mode_includes_untracked_files(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "tracked.py").write_text("def tracked():\n    return True\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=tldr@example.test",
            "-c",
            "user.name=Code Briefcase Tests",
            "commit",
            "-m",
            "seed",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "new_file.py").write_text("def new_symbol():\n    return True\n")

    pack = build_context_pack("", project=tmp_path, changed=True)

    assert any(item.path == "new_file.py" for item in pack.items)
