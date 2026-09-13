# 演示文档

拖进「抽取工作台」即可看到完整效果，不需要自己准备文件。

| 文件 | 用途 | 建议模板 |
|------|------|----------|
| `contract.pdf` | 中文采购合同（有文本层） | 合同 |
| `resume.docx` | 中文简历，**含表格** | 简历 |
| `contract.txt` | `contract.pdf` 的纯文本源文件，方便查看内容 | 合同 |

## 想验证「扫描件」的处理？

`../backend/tests/fixtures/scanned.pdf` 是一份没有文本层的 PDF。
上传它之后：

- 文档可以正常入库，列表里状态显示为「无文本层」，附黄色提示说明原因
- 对它创建抽取任务会**明确失败**并告知"可能是扫描件，请上传电子版"，
  而不是抛一个看不懂的异常，也不是静默返回空结果

## 这些文件是怎么来的？

`contract.pdf` 由 `contract.txt` 经系统的 `cupsfilter` 渲染而成（会嵌入中文字体），
`resume.docx` 由 python-docx 生成。重新生成的方式见
`../backend/tests/fixtures/generate_fixtures.py`。
