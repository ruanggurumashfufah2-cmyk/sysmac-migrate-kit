# AGENTS.md — Sysmac Studio 程序迁移工具包

给任何 AI 编程助手（Claude Code、Codex、Cursor、Gemini CLI 等）看的说明。人看的安装说明见 `README.md`。

## 这是什么
把旧项目的 Omron Sysmac Studio 映射程序段（`xxx_Input刷新` / `xxx_Output刷新`）迁移到新项目：
读 CAD 导出的 PDF 电气图 → 和旧程序按线号对点 → 用户判断 → 生成新程序段 → 写回 Sysmac 剪贴板，用户在 Sysmac 里一次粘贴回去。
**开始前必须先读 `迁移原则.md`**，那是用户确认过的规则，优先级高于你自己的判断。

## 环境
- Windows（读写剪贴板用 Win32 API）；Python 3.10+；`pip install -r requirements.txt`（pymupdf、openpyxl）。
- 所有命令在本目录下执行，并设置 `PYTHONIOENCODING=utf-8`（PowerShell：`$env:PYTHONIOENCODING="utf-8"`）。
- 下文 `<项目>` 指 `项目/<项目名>`，建议每个项目一个目录。

## 流程（按对话推进，每步做完告诉用户下一步）

### 1. 读旧程序（用户说“已复制”）
用户在 Sysmac 左侧 POUs 里 Shift 多选所有 Input/Output 刷新程序段 → 右键「选择并复制多个项目」。然后：
```
python scripts/sysmac-read/read_sysmac_clipboard.py "<项目>/旧程序"
```
报告读到几个程序段。提醒用户复制后不要再复制别的东西。

### 2. 对点 + 生成 + 清算（用户说“对点并生成新程序段”）
```
python scripts/sysmac-io-check/run_all.py "<图纸.pdf>" "<项目>/旧程序" "<项目>/输出"
```
向用户汇报：I/O 页数/点数、对点结果分布、`对点报告.xlsx`「图纸疑点」里标红的、改线号/注释/新增数量、`新程序段/待判断.xlsx` 条数、`新程序段/清算报告.xlsx` 各类数量。

### 3. 用户判断（可多轮；用户说“填好了”）
用户在 `<项目>/输出/对点报告.xlsx`「对点结果」蓝色列「你的判断」「备注」填写（可写自由文字：变量名、需新增、同机头A、按新点位执行、注释掉、保留…），「同义词」表可加叫法。
```
python scripts/sysmac-io-check/learn_feedback.py "<项目>/输出/对点报告.xlsx"
```
文件被 Excel 打开时工具会另存为 `对点报告_时间.xlsx`，读用户实际保存的那个。读完逐条看“带备注的行”：能总结成规律的就改进规则（`scripts/sysmac-io-check/compare_io.py` 的 `interpret` / `NAME_RULES`），并同步写进 `迁移原则.md`，然后重跑第 2 步。

### 4. 粘贴回 Sysmac
告诉用户顺序（先在工程副本上做）：
1. I/O 映射：`对点报告.xlsx`「IO映射粘贴」表，每模块 16 行粘到对应插槽 bit0 起
2. 全局变量：`新程序段/新增变量.txt` 全选复制 → 全局变量表粘贴
3. 程序段：双击 `新程序段/放入剪贴板.bat`，看到【成功】→ Sysmac 右键 POUs →「粘贴多个项目」
4. 编译。用户发报错截图时，按报错修生成规则，重跑第 2 步。

需要你直接写剪贴板时：
```
python scripts/sysmac-read/write_sysmac_clipboard.py "<项目>/旧程序/clipboard_raw.bin" "<项目>/输出/新程序段" --types "<项目>/输出/新程序段/新增变量.txt"
```

## 脚本一览
| 脚本 | 作用 |
|---|---|
| `scripts/sysmac-read/read_sysmac_clipboard.py` | 读 Sysmac 复制到剪贴板的程序段（格式 `Omron.SolutionMultipleRootEntites`），每段导出 `.st.txt`（带 `//====== ST块 N ======` 分隔）+ 变量声明 + 原始数据 |
| `scripts/sysmac-read/write_sysmac_clipboard.py` | 以原始数据为底稿替换改动的 ST 块、补外部变量声明，放回剪贴板（带多重自检，不过就不写） |
| `scripts/sysmac-io-check/extract_drawing_io.py` | PDF 图纸 → I/O 点表（自动找 I/O 页、模拟量、作废/整页作废、线号笔误、工位区更正） |
| `scripts/sysmac-io-check/compare_io.py` | 点表 ↔ 旧程序对点，输出 `对点报告.xlsx`；含图纸自查 `check_drawing.py` |
| `scripts/sysmac-io-check/generate_program.py` | 生成新程序段、新增变量、变更清单、待判断、粘贴助手、放入剪贴板.bat |
| `scripts/sysmac-io-check/audit_program.py` | 清算：奇怪后缀、变量名与图纸不一致、重复赋值、图纸有点程序没用… |
| `scripts/sysmac-io-check/run_all.py` | 一键：提取 → 对点 → 生成 → 清算 |
| `scripts/sysmac-io-check/learn_feedback.py` | 读回用户在报告里的判断，存 `对点反馈.json`，同义词存 `synonyms.json` |

## 注意
- 改规则时同时改脚本和 `迁移原则.md`，保持一致；改完重跑并清算，确认静态检查（空块、空分支、IF 配对、重复赋值）为 0。
- Sysmac 剪贴板数据结构：16 字节 GUID + .NET BinaryFormatter；每个 ST 块的文字存两份（PouBody JSON 的 `TXT` 和 Inline/StructuredText XML），两份都要改；byte[] 长度和 `EncodedDataLength` 要重算；`InstanceHash` 是对象身份哈希不是校验。细节见 `scripts/sysmac-read/write_sysmac_clipboard.py` 文件头。
