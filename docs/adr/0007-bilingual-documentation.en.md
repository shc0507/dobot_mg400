[中文](0007-bilingual-documentation.md)

# 0007: Bilingual documentation

## Status

Accepted: 2026-10-30.

## Context

Lab operators use Chinese and English. Procedures and interface details must stay consistent between both languages.

## Decision

Human documents use Chinese `X.md` and English `X.en.md` beside it. Change both files in the same PR.

- Scope: `README.md`, `docs/*.md`, `docs/adr/*.md`, and `firmware/*/README.md`.
- Agent instructions in `AGENTS.md`, `docs/agents/**`, and `.agents/**` stay English only. `GLOSSARY.md` is 1 bilingual table. `tests/fixtures/**` is exempt.
- Keep the same heading structure, tables, code blocks, numbers, and technical identifiers. Write numbers as digits in both languages. Code comments may be translated.
- The first line links to the other language. English pages link to `.en.md` targets and English anchors.
- `scripts/check_doc_pairs.py` runs through pytest in CI. It checks pair existence, ordered fenced code after comment removal, inline code sets, ASCII numbers outside code, link targets, and heading counts by level.
- Link comparison ignores fragments and treats `.md` and `.en.md` targets as the same document.
- The checker permits missing pairs only through `docs/agents/migration/doc-pairs-pending.txt`. Until a pair exists, edit the Chinese file. Remove its pending entry when you add the English pair.
- Write Chinese with `jianming-zhongwen` and English with `asd-ste100`. Keep instructions concise and current. Store run outcomes in issue comments, not instruction pages.

## Consequences

CI detects structural and technical drift. Reviewers still check translation meaning, because matching structure does not prove equivalent instructions.
