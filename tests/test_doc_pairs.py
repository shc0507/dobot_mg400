"""Document pair checks run offline against the repository and temporary documents."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_doc_pairs.py"
CHECK = runpy.run_path(str(SCRIPT))


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def pair(root, chinese, english, name="docs/example.md"):
    write(root, name, chinese)
    write(root, name[:-3] + ".en.md", english)


def test_repository_document_pairs():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "chinese,english",
    [
        ("```sh\nrun\n```\n```json\n{}\n```", "~~~sh\nrun\n~~~\n```json\n{}\n```"),
        ("```python\n# 注释\nvalue = 3  # 数量\n```", "```python\nvalue = 3 # Count\n```"),
        ("```js\n// 注释\nrun(); // 运行\n```", "```js\n// Comment\nrun(); // Run\n```"),
        ("`port` 和 ``a`b``。", "Use ``a`b`` and `port`, then `port`."),
        ("` value `", "``value``"),
        ("上限 10，偏移 -1.5。", "Offset -1.5, maximum 10, repeated 10."),
        ("代码 `999`，容量 20。", "Code `999`, capacity 20."),
        ("[部署](deploy.md#安装)", "[Deploy](deploy.en.md#installation)"),
        ("[English](example.en.md)", "[中文](example.md)"),
        ("[说明][guide]\n\n[guide]: guide.md#说明", "[Guide](guide.en.md#guide)"),
        ("[图](<image(1).png>)", "[Image](image\\(1\\).png)"),
        ("<https://example.org/docs#介绍>", "<https://example.org/docs#introduction>"),
        ("# 标题\n## 小节\n## 另一节", "# Title\n## Another section\n## Section"),
        ("标题\n===\n\n小节\n---", "# Title\n\n## Section"),
    ],
)
def test_matching_pairs(tmp_path, chinese, english):
    pair(tmp_path, chinese, english)
    assert CHECK["check_repository"](tmp_path) == []


@pytest.mark.parametrize(
    "chinese,english,field,left,right",
    [
        ("```sh\none\n```", "```sh\none\n```\n```sh\ntwo\n```", "fenced code blocks", "one", "two"),
        (
            "```sh\none\n```\n```sh\ntwo\n```",
            "```sh\ntwo\n```\n```sh\none\n```",
            "fenced code blocks",
            "one",
            "two",
        ),
        (
            "```sh\nrun --slow\n```",
            "```sh\nrun --fast\n```",
            "fenced code blocks",
            "--slow",
            "--fast",
        ),
        ("```python\nx = 1\n```", "```text\nx = 1\n```", "fenced code blocks", "python", "text"),
        ("`port`", "`host`", "inline code spans", "port", "host"),
        ("上限 10。", "Maximum 20.", "ASCII numbers outside code", "10", "20"),
        ("偏移 -1.5。", "Offset 1.5.", "ASCII numbers outside code", "-1.5", "1.5"),
        ("上限 １２。", "Maximum 12.", "ASCII numbers outside code", "[]", "12"),
        ("[部署](deploy.md)", "[Deploy](other.en.md)", "link targets", "deploy.md", "other.md"),
        ("[图](image.png)", "[Image](other.png)", "link targets", "image.png", "other.png"),
        ("# 标题\n## 小节", "# Title\n### Section", "headings by level", "2", "3"),
        ("## 小节\n## 小节", "## Section", "headings by level", "2: 2", "2: 1"),
    ],
)
def test_mismatches_name_both_files_and_differences(tmp_path, chinese, english, field, left, right):
    pair(tmp_path, chinese, english)
    messages = CHECK["check_repository"](tmp_path)
    message = next(message for message in messages if field in message)
    assert "docs/example.md" in message and "docs/example.en.md" in message
    assert left in message and right in message


@pytest.mark.parametrize("marker", ["#", "//"])
def test_comment_markers_inside_strings_remain_significant(tmp_path, marker):
    pair(
        tmp_path,
        f'```text\nvalue = "{marker} one"\n```',
        f'```text\nvalue = "{marker} two"\n```',
    )
    assert any("fenced code blocks" in item for item in CHECK["check_repository"](tmp_path))


def test_fence_contents_do_not_count_as_prose_or_inline_code(tmp_path):
    pair(
        tmp_path,
        "```text\n# 注释 99 `left` [左](left.md)\n```\n正文 1。",
        "```text\n// Comment 22 `right` [Right](right.md)\n```\nText 1.",
    )
    assert CHECK["check_repository"](tmp_path) == []


def test_nested_fence_is_preserved(tmp_path):
    pair(tmp_path, "````text\n```\nvalue\n```\n````", "````text\n```\nother\n```\n````")
    assert any("fenced code blocks" in item for item in CHECK["check_repository"](tmp_path))


def test_unclosed_fence_is_reported(tmp_path):
    pair(tmp_path, "```sh\nrun", "```sh\nrun\n```")
    assert any(
        "Chinese document: unclosed code fence" in item
        for item in CHECK["check_repository"](tmp_path)
    )


@pytest.mark.parametrize(
    "name",
    ["README.md", "docs/new.md", "docs/adr/0001-service.md", "firmware/demo/README.md"],
)
def test_scope_requires_missing_pair(tmp_path, name):
    write(tmp_path, name, "# 标题")
    assert CHECK["check_repository"](tmp_path) == [
        f"{name} <-> {name[:-3]}.en.md: missing pair file {name[:-3]}.en.md"
    ]


def test_orphan_english_file_requires_chinese_pair(tmp_path):
    write(tmp_path, "README.en.md", "# Title")
    assert CHECK["check_repository"](tmp_path) == [
        "README.md <-> README.en.md: missing pair file README.md"
    ]


def test_pending_list_only_exempts_missing_pairs(tmp_path):
    write(tmp_path, "docs/example.md", "# 标题 10")
    write(tmp_path, CHECK["PENDING"], "docs/example.md\n")
    assert CHECK["check_repository"](tmp_path) == []
    write(tmp_path, "docs/example.en.md", "# Title 20")
    assert any("ASCII numbers" in item for item in CHECK["check_repository"](tmp_path))


def test_pending_entry_does_not_exempt_another_document(tmp_path):
    write(tmp_path, "docs/example.md", "# 标题")
    write(tmp_path, CHECK["PENDING"], "docs/other.md\n")
    assert len(CHECK["check_repository"](tmp_path)) == 1


def test_exempt_paths_are_ignored(tmp_path):
    for name in (
        "AGENTS.md",
        "GLOSSARY.md",
        "docs/agents/example.md",
        ".agents/skills/example/SKILL.md",
        "tests/fixtures/example.md",
        "firmware/demo/other.md",
    ):
        write(tmp_path, name, "# No pair")
    assert CHECK["check_repository"](tmp_path) == []


def test_cli_prints_every_mismatch_and_fails(tmp_path, capsys):
    pair(tmp_path, "# 标题\n`left` 10", "## Title\n`right` 20")
    assert CHECK["main"](["--root", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert output.count("docs/example.md <-> docs/example.en.md:") == 3
    assert "inline code spans" in output
    assert "ASCII numbers outside code" in output
    assert "headings by level" in output


def test_cli_succeeds_for_matching_pair(tmp_path, capsys):
    pair(tmp_path, "# 标题", "# Title")
    assert CHECK["main"](["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "language,chinese,english",
    [
        ("python", "value = 10 // 2", "value = 10 // 3"),
        ("cpp", "#define LIMIT 10", "#define LIMIT 20"),
        ("sh", "echo ${value#prefix}", "echo ${value#other}"),
    ],
)
def test_operators_and_directives_are_not_comments(tmp_path, language, chinese, english):
    pair(tmp_path, f"```{language}\n{chinese}\n```", f"```{language}\n{english}\n```")
    assert any("fenced code blocks" in item for item in CHECK["check_repository"](tmp_path))


@pytest.mark.parametrize("language", ["js", "cpp"])
def test_adjacent_slash_comments_can_be_translated(tmp_path, language):
    pair(
        tmp_path,
        f'```{language}\nrun("https://example.org");// 运行\n```',
        f'```{language}\nrun("https://example.org");// Run\n```',
    )
    assert CHECK["check_repository"](tmp_path) == []
    write(tmp_path, "docs/example.en.md", f'```{language}\nrun("https://other.org");// Run\n```')
    assert any("fenced code blocks" in item for item in CHECK["check_repository"](tmp_path))
