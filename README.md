# shell module

在白名单工作目录内执行**单个命令**，不经过 shell。模块的价值不在 `exec`，
而在四条边界：**cwd**（越界拒绝）、**无 shell**（管道/拼接拒绝而不是当参数传下去）、
**时间**（超时杀整个进程组并返回 `timed_out`）、**大小**（每个流 64 KiB，超出部分
边读边丢，绝不因写满管道而卡死）。

## 工具

| 工具 | 签名 | 结果 | risk | idempotent |
|---|---|---|---|---|
| `shell_exec` | `shell_exec(command: str, timeout_s: int = 30, cwd: str = ".")` | `{exit_code, stdout, stderr, truncated, timed_out}` | **high** | **false** |
| `shell_which` | `shell_which(name: str)` | `{path}`（未找到为 `null`） | low | true |

`shell_exec` 的返回约定：

- `exit_code`：子进程退出码。**非零不是错误**，是一次正常返回；只有"没能启动"
  或"参数不合法"才是错误。
- `stdout` / `stderr`：UTF-8 文本，非法字节替换为 `U+FFFD`（命令可能输出二进制，
  这不是失败的理由）。
- `truncated`：任一流超过 64 KiB 时为 `true`。此时命令**仍然跑完了**，只是返回的
  副本变短。
- `timed_out`：**总是存在**。超时为 `true`，此时 `exit_code` 是 `-9`（SIGKILL）。
  超时不抛错——模型需要看到"它没跑完"，而不是一个看不出发生什么的异常。

`shell_which` 回答的是"`shell_exec` 会用哪个二进制"，因此和 `shell_exec` 用同一条
PATH：`{path: null}` 表示 PATH 里没有可执行的同名文件，这是关于主机的事实，不是调用失败。

## 安全模型

### 1. cwd 白名单

```bash
SHELL_ROOTS=/data/workspace:/srv/scratch   # ':' 分隔，按声明顺序匹配
SHELL_DATA_DIR=/data/workspace             # 未设置 SHELL_ROOTS 时的默认根
```

- **每次调用**重新读环境变量并 `resolve()` 根（根本身可以是软链）。
- 两个都没设 → 所有 `shell_exec` 都 `PERMISSION_DENIED`（fail closed）。
- 相对 `cwd` 相对**第一个**根解释；`""` / `"."` / `"./"` 都是第一个根。
- 判断用 `Path.resolve()` + `Path.is_relative_to`，**不用字符串前缀**：
  `/data/ws-evil` 不会骗过 `/data/ws`（`test_sibling_with_the_same_prefix_is_denied`）。
- 越界判断在存在性判断**之前**：根外一个不存在的路径返回 `PERMISSION_DENIED`，
  而不是 `NOT_FOUND`——错误信息不能变成"根外什么存在"的探针。

> **cwd 是起点，不是牢笼。** 命令自己仍可 `cd ..`、仍可读它 uid 能读的任何文件。
> 真正的隔离来自容器：uid 10001、`/data` 之外没有可写层、必要时 `--network=none`。
> 本模块不会假装自己比这更强。

### 2. 不经过 shell

命令字符串用 `shlex.split`（POSIX 规则）拆成 argv，再 `Popen(argv)`——
**永远不是** `shell=True`，因此不存在注入面。但仍然**拒绝** shell 语法，而不是
把 `|` 当成普通参数传下去：

| 写法 | 结果 |
|---|---|
| `ls \| grep x`、`a; b`、`a && b`、`a &` | `INVALID_ARGUMENT` |
| `>`、`<` 重定向 | `INVALID_ARGUMENT` |
| `$(...)`、反引号 | `INVALID_ARGUMENT` |
| 未加引号的 `$HOME` | `INVALID_ARGUMENT`（我们不做展开，静默当字面量会误导） |
| `'a|b;c'`（引号内） | 合法，是一个普通参数 |
| `"$HOME"`（双引号内） | `INVALID_ARGUMENT`（shell 本会展开，我们不会） |
| `echo *` | 合法，输出字面量 `*`（不做 glob） |

扫描器是引号感知的：单引号内一切按字面量处理，所以 `grep 'a|b'` 这种正常参数
不会被打扰。代价是 `sh -c "..."` 也被拒绝（`sh` 会把所有元字符带回来）——需要
多步动作时，把脚本文件放进根目录里再执行它，这是本版有意的边界。

### 3. 子进程环境

子进程**不继承**本进程的环境。它只拿到：

```
PATH（父进程的 PATH，或 /usr/local/bin:/usr/bin:/bin）
HOME=cwd   TMPDIR=cwd   TERM=dumb   LANG/LC_ALL/TZ（若设置）
```

理由：命令不能靠 `env` 读到 `MODULE_KEY_PATH`、`CORE_PUBLIC_KEY` 或任何 `SHELL_*`；
`HOME`/`TMPDIR` 指向解析后的 cwd，所以 `~/.cache` 和 `mktemp` 落在沙箱里。
PATH 里永远没有空串 / `.`，否则沙箱内一个同名文件就能顶替真命令。

### 4. 超时与进程组

`timeout_s` 必须是整数，范围 `[1, 120]`（`true`、`"5"`、`1.5` 都拒绝）。

子进程用 `start_new_session=True` 启动，因此自己是一个进程组组长。超时后
`os.killpg(pid, SIGKILL)`——**孙进程一起死**，不会留下后台孤儿继续跑。
`test_timeout_also_reaps_a_grandchild` 覆盖这条。

杀完最多等 5 秒；然后对两个读线程做**有界 join**（5 秒）。如果某个逃出进程组的
孙进程还握着管道，调用也必须返回——"输出停了"不能变成"调用挂了"。

### 5. 输出上限

| 上限 | 值 | 行为 |
|---|---|---|
| stdout | 64 KiB | 保留前 64 KiB，`truncated=true` |
| stderr | 64 KiB | 同上（两个独立配额） |
| 命令字符串 | 32 KiB | `PAYLOAD_TOO_LARGE` |

**截断之后仍然读到 EOF**：管道被读空、多余字节丢弃。若在 64 KiB 处停止读取，子进程
会写满管道后永久阻塞，`wait()` 就再也不会返回。`test_a_flooding_command_still_finishes`
用 4 MB 输出钉住这条（并且断言耗时正常，不是"刚好没超时"）。

### 6. 可选命令白名单

```bash
SHELL_ALLOWLIST=python3:pytest:git    # ':' 分隔；空/未设置 = 不限制
```

- 取 `shlex.split` 后的 `argv[0]` 的 **basename** 比对：`/usr/local/bin/python3`
  按 `python3` 判定，绝对路径不能绕过白名单。
- 白名单条目自身也归一化成 basename：写 `/usr/bin/curl` 等于写 `curl`（否则它永远
  匹配不上，静默拒绝一个本来想放行的命令）。
- 空列表 = 不限制，**由 core 审批把关**。这一栏是运维的收口，不是本模块的审批层。
- `shell_which` 同样受白名单约束，否则它会变成"白名单外有哪些命令"的探测面。

> **白名单不是沙箱。** 放行 `python3` 等于放行任意代码（`python3 -c ...` 就是完整
> 逃逸）；根目录里放一个同名可执行文件也能被 `./python3` 调用，因为比对的是
> basename。这个开关只回答"这台机器上允许出现哪些命令名"，不回答"命令能做什么"。
> 后一个问题由容器与 core 审批回答。

### 7. 错误码与信息

| 码 | 何时 |
|---|---|
| `INVALID_ARGUMENT` | 参数类型不对（含 `bool` 冒充 int）、命令含 NUL/空、含 shell 语法、引号不闭合、`timeout_s` 越界、cwd 存在但不是目录、`which` 传了路径 |
| `PERMISSION_DENIED` | 未配置根、cwd 越界（`..` / 绝对路径 / 软链）、命令不在 `SHELL_ALLOWLIST`、命令不可执行、OS 权限拒绝 |
| `NOT_FOUND` | cwd 不存在、命令在 PATH 中找不到 |
| `PAYLOAD_TOO_LARGE` | 命令字符串 > 32 KiB |
| `INTERNAL` | 未归类的启动失败（只带 `strerror`，不带路径） |

**错误信息里没有绝对路径**：越界错误连调用方给的路径都不回显；`NOT_FOUND` /
`INVALID_ARGUMENT` 只带**相对根**的 cwd（根显示为 `"."`）。环境变量也从不进入
错误文本。`test_errors_never_leak_absolute_paths` /
`test_error_messages_do_not_contain_the_environment` 钉住这两条。

## Frame ceiling（部署前必读）

v1 传输没有分片，一帧明文上限 65519 字节（`limits.max_payload`）。本模块
stdout/stderr **各 64 KiB 是本地上限**（防内存与上下文放大），但 64 KiB 的单个流
JSON 包装后就已经超过一帧：这样的结果**发不出去**，transport 会以
`PAYLOAD_TOO_LARGE` 断开（fatal）。

所以连 core 部署时，单次调用实际可送达的输出约 **60 KiB 总量**（stdout + stderr +
JSON 包装共用一帧）。这是有意的取舍：截断阈值不随部署方式变化，`truncated` 的语义
在哪里都一致。需要更多输出时，让命令把结果写进根目录里的文件，再用 `fs_read`
分块读回来——shell 只负责跑，不负责搬运大块数据。

`--standalone` 不经过 transport，不受此限。

## 布局

```
modules/shell/
├── manifest.json            generated — never hand-edited
├── pyproject.toml
├── src/shell_module/
│   ├── tools.py             pure functions, stdlib only, no SDK import
│   └── server.py            ModuleApp + ToolSpec, only forwards
├── tests/test_tools.py      runs without vade-sdk installed
├── tests/test_server.py     skipped when vade-sdk is missing
├── Dockerfile               build from the REPO ROOT (needs sdk/python)
└── data/                    module key lives here (gitignored, volume it)
```

`tools.py` 不 import `vade_sdk`（AST 测试钉住），所以工具可以在没有 SDK 的机器上
写完、审完、测完；`server.py` 不碰路径、不碰进程，只做
`ToolFailure → ToolError` 的翻译。`ToolSpec` 由 SDK 从函数签名生成 JSON Schema ——
签名就是 schema，默认值直接进 manifest。

`concurrency="pooled"` + `max_concurrency=2`：构建和探测可以并行，但共享 cwd 的
副作用不会无限放大；`shell_exec` 不是幂等的。每个调用仍受自己 120 秒上限约束。

## 运行

```bash
pip install -e sdk/python          # 或 pip install "vade-sdk==0.1.*"
pip install -e modules/shell

pytest modules/shell/tests -q      # 77 passed（无 core；无 SDK 时 server 测试 skip）
python -m shell_module --check     # exit 0，并把 manifest 打到 stdout

SHELL_ROOTS=/data/workspace \
  python -m shell_module --standalone shell_exec --args '{"command":"python3 -c \"print(1)\""}'
# {"ok": true, "result": {"exit_code": 0, "stdout": "1\n", ...}}

SHELL_ROOTS=/data/workspace \
  python -m shell_module --standalone shell_exec --args '{"command":"ls | wc -l"}'
# {"ok": false, "error": {"code": "INVALID_ARGUMENT", ...}}   exit 1  —— 不支持管道

SHELL_ROOTS=/data/workspace \
  python -m shell_module --standalone shell_exec --args '{"command":"true","cwd":"../../etc"}'
# {"ok": false, "error": {"code": "PERMISSION_DENIED", ...}}   exit 1
```

`manifest.json` 由同一条生成路径产出（`--print-manifest` / `--check` /
`module/register` 共用一个函数）：

```bash
PYTHONPATH=src python -m shell_module --print-manifest > manifest.json
PYTHONPATH=src python -m shell_module --print-manifest | diff - manifest.json   # 空
```

连 core（需要 core 白名单里有这个模块的 key，且 core 的 Noise 公钥已知）：

```bash
CORE_PUBLIC_KEY=<64 hex> MODULE_KEY_PATH=data/module.key \
  SHELL_ROOTS=/data/workspace SHELL_ALLOWLIST=python3:git:pytest \
  python -m shell_module --core wss://127.0.0.1:8443
```

## 容器

```bash
docker build -f modules/shell/Dockerfile -t vade/shell:dev .
docker run --rm -v /tmp/agent:/data/workspace --network=none vade/shell:dev \
  --standalone shell_exec --args '{"command":"python3 --version"}'
```

镜像里 `SHELL_DATA_DIR=SHELL_ROOTS=/data/workspace`，uid 10001，`/data` 之外没有
可写层。**跑命令的容器要按"不可信代码"部署**：`--network=none`（命令不需要网络时）、
`--read-only`、`--pids-limit`、`--memory`，并且 `MODULE_KEY_PATH` 必须挂卷持久化，
否则指纹变化会被 core 拒绝（AMEND-01）。

## 已知限制（有意为之）

1. **没有管道**。`a | b` 被拒绝。多步动作用根目录里的脚本。
2. **没有 `sh -c`**。它会带回所有元字符；同样用脚本。
3. **没有 stdin**。子进程的 stdin 是 `/dev/null`，不会挂住等待输入。
4. **cwd 不是沙箱**（见 §1）。隔离靠容器。
5. **同步工具无法被 core 真正取消**：`tools/cancel` 只是标记，进程会跑到自己的
   超时（≤120 秒）。这是 SDK 对同步工具的既有语义，不是本模块的例外。
6. **输出不是流式**：拿不到"还没结束时的输出"，只有结束时的一段快照。

## 两条不变量

1. `tools.py` 零 SDK import（AST 测试），`server.py` 零业务逻辑。
2. 每个 `shell_exec` 在 spawn 之前一定走完 `split_command()` → `_check_allowed()`
   → `resolve_cwd()`；没有任何一条路径能跳过 cwd 检查。
