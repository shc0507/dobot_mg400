"""Check the structure and technical content of Chinese and English document pairs."""

import argparse
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PENDING = "docs/agents/migration/doc-pairs-pending.txt"
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.DOTALL)
NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)*(?:[eE][+-]?[0-9]+)?")
HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
REFERENCE = re.compile(r"^ {0,3}\[[^]\n]+\]:[ \t]*", re.MULTILINE)
AUTOLINK = re.compile(r"<((?:[A-Za-z][A-Za-z0-9+.-]*:|[^<>\s]+@)[^<>\s]*)>")


def blank(text):
    """Mask syntax without changing the line boundaries."""
    return "".join("\n" if char == "\n" else " " for char in text)


def code_lines(lines, language):
    """Remove comments while preserving quoted text and language operators."""
    language = language.split()[0].lower() if language else ""
    if language in {"python", "py", "sh", "bash", "shell", "zsh", "toml", "yaml", "yml", "ini"}:
        markers = ("#",)
    elif language in {"c", "cpp", "c++", "js", "javascript", "ts", "typescript", "java", "rust"}:
        markers = ("//",)
    elif language == "json":
        markers = ()
    else:
        markers = ("#", "//")
    result = []
    quote = None
    for line in lines:
        index = 0
        comment = None
        while index < len(line):
            char = line[index]
            if char == "\\":
                index += 2
                continue
            if quote:
                if line.startswith(quote, index):
                    index += len(quote)
                    quote = None
                    continue
            elif char in "\"'`":
                quote = char * 3 if line.startswith(char * 3, index) else char
                index += len(quote)
                continue
            elif any(line.startswith(marker, index) for marker in markers) and (
                index == 0
                or line[index - 1].isspace()
                or language in {"python", "py"}
                or markers == ("//",)
            ):
                comment = index
                break
            index += 1
        if comment is not None:
            line = line[:comment].rstrip()
            if not line.strip():
                continue
        result.append(line.rstrip())
    return tuple(result)


def split_fences(text):
    prose, blocks, errors = [], [], []
    marker = None
    lines = []
    language = ""
    for line in text.splitlines():
        match = FENCE.match(line)
        if marker is None:
            if match:
                marker, language = match.groups()
                language = language.strip()
                lines = []
                prose.append("")
            else:
                prose.append(line)
        elif (
            match
            and match[1][0] == marker[0]
            and len(match[1]) >= len(marker)
            and not match[2].strip()
        ):
            blocks.append((language, code_lines(lines, language)))
            marker = None
            prose.append("")
        else:
            lines.append(line)
            prose.append("")
    if marker is not None:
        errors.append("unclosed code fence")
    return "\n".join(prose), blocks, errors


def link_target(text, start):
    """Read a Markdown destination, including escaped or balanced parentheses."""
    start += len(text[start:]) - len(text[start:].lstrip())
    if start < len(text) and text[start] == "<":
        end = text.find(">", start + 1)
        if end != -1:
            return text[start + 1 : end], end + 1
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char.isspace() or (char == ")" and depth == 0):
            break
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return text[start:index], index


def links_and_prose(text):
    targets, spans = set(), []
    starts = [match.end() for match in re.finditer(r"(?<!\\)\]\(", text)]
    starts.extend(match.end() for match in REFERENCE.finditer(text))
    for start in starts:
        target, end = link_target(text, start)
        targets.add(normalize_target(target))
        spans.append((start, end))
    for match in AUTOLINK.finditer(text):
        targets.add(normalize_target(match[1]))
        spans.append(match.span())
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + blank(text[start:end]) + text[end:]
    return targets, text


def normalize_target(target):
    target = re.sub(r"\\([\\()<>])", r"\1", target).split("#", 1)[0]
    return re.sub(r"\.en\.md(?=$|\?)", ".md", target)


def features(text):
    prose, blocks, errors = split_fences(text)
    prose = re.sub(r"<!--.*?-->", lambda match: blank(match[0]), prose, flags=re.DOTALL)
    spans = set()

    def remove_code(match):
        value = match[2].replace("\n", " ")
        if value.startswith(" ") and value.endswith(" ") and value.strip():
            value = value[1:-1]
        spans.add(value)
        return blank(match[0])

    prose = INLINE_CODE.sub(remove_code, prose)
    targets, prose = links_and_prose(prose)
    headings = Counter()
    lines = prose.splitlines()
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match:
            headings[len(match[1])] += 1
        elif index and lines[index - 1].strip() and re.fullmatch(r" {0,3}(=+|-+)[ \t]*", line):
            headings[1 if line.lstrip().startswith("=") else 2] += 1
    return {
        "fenced code blocks": blocks,
        "inline code spans": spans,
        "ASCII numbers outside code": set(NUMBER.findall(prose)),
        "link targets": targets,
        "headings by level": dict(headings),
    }, errors


def document_pairs(root):
    paths = set()
    for pattern in (
        "README.md",
        "README.en.md",
        "docs/*.md",
        "docs/adr/*.md",
        "firmware/*/README.md",
        "firmware/*/README.en.md",
    ):
        for path in root.glob(pattern):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                paths.add(re.sub(r"\.en\.md$", ".md", relative))
    return [(name, name[:-3] + ".en.md") for name in sorted(paths)]


def differences(chinese, english):
    left, left_errors = features(chinese)
    right, right_errors = features(english)
    errors = [f"Chinese document: {error}" for error in left_errors]
    errors.extend(f"English document: {error}" for error in right_errors)
    for field in left:
        if left[field] == right[field]:
            continue
        if isinstance(left[field], set):
            errors.append(
                f"{field}: Chinese only {sorted(left[field] - right[field])!r}; "
                f"English only {sorted(right[field] - left[field])!r}"
            )
        else:
            errors.append(f"{field}: Chinese {left[field]!r}; English {right[field]!r}")
    return errors


def check_repository(root):
    pending_path = root / PENDING
    pending = (
        set(pending_path.read_text(encoding="utf-8").splitlines())
        if pending_path.exists()
        else set()
    )
    errors = []
    for chinese, english in document_pairs(root):
        missing = [name for name in (chinese, english) if not (root / name).is_file()]
        if missing:
            if chinese not in pending and english not in pending:
                errors.append(f"{chinese} <-> {english}: missing pair file {missing[0]}")
            continue
        messages = differences(
            (root / chinese).read_text(encoding="utf-8"),
            (root / english).read_text(encoding="utf-8"),
        )
        errors.extend(f"{chinese} <-> {english}: {message}" for message in messages)
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root to check")
    args = parser.parse_args(argv)
    errors = check_repository(args.root)
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
