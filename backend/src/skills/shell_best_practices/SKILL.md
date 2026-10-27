---
name: shell_best_practices
description: 在 bash 工具里高效、安全地执行命令：非交互、控制输出、避免被拦或被审批
triggers: ["命令行", "shell", "bash", "终端命令", "运行命令"]
required_tools: [bash]
---

# Shell 使用规范

`bash` 在工作区根目录执行命令，输出会被蒸馏（头尾加关键行），全文可用 `read_artifact` 取回。

## 必须非交互
- 没有标准输入：不要运行需要交互的命令（不带参数的 `python`、`vim`、`less`、不带 `-m` 的 `git commit`、`npm init`、`ssh`）。
- 分页器会卡住：`git --no-pager log -n 20`，或在后面加 `| cat`。
- 需要确认的命令加非交互开关（`-y`、`--yes`、`--no-input`）。

## 控制输出体积
- 先估量再读：`wc -l file`、`ls -la`、`git status --short`。
- 大文件不要 `cat`：读片段用 `view_file`，或 `sed -n '120,180p' file`、`head -n 50`。
- 搜索加限制：优先用 `grep` 工具；命令行里用 `rg -n --max-count 5 pattern` 或 `| head -n 50`。
- 编译与测试只关心结论：`pytest -q -x`，失败时再看详情。

## 组合命令
- 一行里串联相关的只读命令比分多轮调用省轮次：`ls && git status --short && head -n 20 README.md`。
- **不要把有副作用的操作和只读探测混在一条里**：权限按整条命令分类，混在一起会整体升级到更高档，触发不必要的审批。

## 避免被拦或被审批
| 想做的事 | 建议 |
|---|---|
| 写文件 | 用 `write_file` 或 `edit_file`，不要 `echo > file` / `cat <<EOF > file` |
| 删除 | 删单个具体文件，避免 `rm -rf` 加通配 |
| 联网 | 不要自己 `curl`；外部资料用 `delegate_research` |
| 安装依赖 | 需要审批；先确认确实缺依赖（读 `pyproject.toml`、`package.json`） |
| `git push`、`sudo`、`docker` | 需要全权限审批 |

被拒绝后**不要**换个写法重试（拆分命令、改写参数、借脚本间接执行）：这会被视为违规。改用合规方案，或如实报告做不了。

## 长时间运行的命令
- 默认 60 秒超时；构建与测试更久时用 `timeout_sec` 放宽。
- 不要启动常驻进程（开发服务器、监听器）：没有后台执行，进程会占着沙箱槽位直到超时被杀。要验证服务，用一条命令完成「启动、探测、退出」并带 `timeout`。
- 收到「已进入等待队列」时稍后重试或减少并行，不要立刻重发。

## 命令失败时
先看报错的最后几行与关键行，不要重复同一条命令（连续三次相同调用会被拦截）。退出码非零但输出为空，多半是命令本身有问题。
