[English](0007-bilingual-documentation.en.md)

# 0007：双语文档

## 状态

已接受：2026-10-30。

## 背景

实验室操作人员使用中文和英文。两种语言的流程与接口细节必须一致。

## 决定

人员文档使用中文 `X.md` 和同目录的英文 `X.en.md`。在同一个 PR 中修改两份文件。

- 范围：`README.md`、`docs/*.md`、`docs/adr/*.md` 和 `firmware/*/README.md`。
- `AGENTS.md`、`docs/agents/**` 和 `.agents/**` 中的智能体说明仅用英文。`GLOSSARY.md` 使用 1 张双语表。`tests/fixtures/**` 不受配对规则约束。
- 保持标题结构、表格、代码块、数字和技术标识一致。两种语言均使用阿拉伯数字。代码注释可以翻译。
- 首行链接到另一种语言。英文页面使用 `.en.md` 目标和英文锚点。
- CI 通过 pytest 运行 `scripts/check_doc_pairs.py`。检查内容包括配对文件、去除注释后的有序围栏代码块、行内代码集合、代码外 ASCII 数字、链接目标和各级标题数量。
- 链接比较忽略片段，并将 `.md` 与 `.en.md` 目标视为同一文档。
- 缺失配对文件仅通过 `docs/agents/migration/doc-pairs-pending.txt` 豁免。配对文件创建前，修改中文文件。添加英文配对文件时，删除对应待办条目。
- 中文使用 `jianming-zhongwen`，英文使用 `asd-ste100`。说明保持简洁且符合当前流程。运行结果写入议题评论，不写入操作说明。

## 影响

CI 检测结构与技术内容的差异。审阅者仍须检查译文含义，因为结构一致不能证明指令等价。
