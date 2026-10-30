# zzb-codex-ui

常驻终端工作台：左侧文件树显示目录与普通文件，中央完整原版 zzb-codex 对话（含原版 status line），右侧历史会话与 Summary。中央占主要宽度，左右侧栏常驻。使用 Python 3 标准库和 tmux，支持 macOS 与 Linux。

```text
┌──────────┬────────────────────────────────┬──────────────┐
│ 文件树   │                                │ 历史会话     │
│          │      原版 zzb-codex 对话       │              │
│          │                                ├──────────────┤
│          │                                │ Summary      │
│          │ 原版输入区 + status line       │ 模型/用量等  │
└──────────┴────────────────────────────────┴──────────────┘
```

侧栏、分隔线和工作台底栏采用低饱和炭灰主题：目录为灰蓝，会话为柔紫，模型为灰紫，用量为灰绿，时间为暖棕，内容预览为浅灰；选中行使用深灰底，并保留不同信息的颜色。支持 256 色终端，低色彩终端自动降为清晰的黑白样式。中央对话及原版 status line 继续由 zzb-codex 自身绘制。

## 启动

```sh
zzb-codex-ui                          # 当前目录的工作台
zzb-codex-ui resume                   # 同上
zzb-codex-ui -C ~/Documents           # 指定目录
zzb-codex-ui resume -C ~/Documents
zzb-codex-ui resume SESSION_ID        # 常驻布局内恢复指定会话
zzb-codex-ui resume --last            # 常驻布局内恢复最近会话
zzb-codex-ui resume --all             # 中央显示原版跨目录选择器
```

`resume` 后的非选项参数是会话 ID/名称；`./resume` 表示本地目录。原有 `zzb-codex-ui 目录` 仍可用。目录位置参数与 `-C`/`--cd` 不能同时使用。

`exec`、`login`、`mcp` 等非交互管理命令，以及 `--version`、`resume --help` 仍直接交给原版启动器；参数保持原样。交互会话继续使用原有 zzb-codex 启动器的配置与环境。

## 依赖与平台

需要 Python 3（仅标准库）和 tmux：

- macOS：`brew install tmux`
- Debian/Ubuntu 等 Linux：`sudo apt install tmux`（其他发行版使用对应包管理器）
- tmux 不在 `PATH` 时，可用环境变量 `ZZB_TMUX` 指定可执行文件；未找到时会提示上述安装命令。

复制路径（`y`、鼠标双击，或左侧文件上按 `Enter`）只调用本机剪贴板工具，不读取文件内容，不使用 OSC 52，也不进行外部网络复制：

- macOS：使用系统自带 `pbcopy`。
- Linux Wayland：优先使用 `wl-copy`（安装：`sudo apt install wl-clipboard`）。
- Linux X11：优先使用 `xclip -selection clipboard`（安装：`sudo apt install xclip`），没有 `xclip` 时使用 `xsel --clipboard --input`（安装：`sudo apt install xsel`）。
- `DISPLAY` 和 `WAYLAND_DISPLAY` 都没有设置的纯终端/SSH 会话会明确报错，不会尝试启动 X 工具。工具缺失或执行失败会显示安装建议或错误信息，不会误报复制成功。

## 可配置路径

以下环境变量在启动时读取，不会使用 `CODEX_HOME`：

- `ZZB_CODEX_HOME`：状态目录，默认 `~/.zzb-codex`；数据库位于 `$ZZB_CODEX_HOME/db/state_5.sqlite`。
- `ZZB_CODEX_PREFIX`：安装前缀，默认 `~/local`；默认启动器为 `$ZZB_CODEX_PREFIX/bin/zzb-codex`。
- `ZZB_CODEX_LAUNCHER`：直接指定 zzb-codex 启动器，优先于 `ZZB_CODEX_PREFIX`。
- `ZZB_TMUX`：直接指定 tmux 可执行文件，覆盖 `PATH` 中的自动检测。

## 操作

| 按键或鼠标 | 操作 |
| --- | --- |
| `F6`，或点击对应面板 | 循环切换文件树、对话、历史三栏 |
| `Tab` / `Shift+Tab`（侧栏内） | 在左右侧栏之间切换 |
| `j/k` 或上下方向键 | 在文件和目录间移动选择 |
| `h/l` 或左右方向键 | 折叠/展开目录；文件上 `l` 不操作，`h` 回到父目录 |
| `Enter` | 目录展开/折叠；左侧文件复制绝对路径；会话打开/切换 |
| 单击目录 / 单击会话 | 展开目录 / 切换会话 |
| 单击文件 | 只选择文件，不展开或启动对话 |
| 双击文件或目录，或 `y` | 复制所选文件或目录的绝对路径 |
| `n` | 在当前目录新建对话 |
| `.` | 显示/隐藏以 `.` 开头的文件和目录 |
| `g/G`、`PgUp/PgDn` | 首尾和翻页 |
| `r` | 刷新文件树和会话；会话列表也会定期刷新 |
| `?` | 帮助 |
| `q`（侧栏内） | 暂离工作台，保留对话进程 |

文件树逐层显示目录、普通文件和符号链接，每层目录优先，其余按大小写无关的名称排序。以 `.` 开头的文件和目录统一由 `.` 显示或隐藏。`y`、鼠标双击，或左侧焦点下对文件按 `Enter`，会复制该节点的绝对路径；文件符号链接复制链接自身路径。

文件夹使用灰蓝色和展开箭头，文件使用 `·` 标记并按类型配色：代码/脚本灰绿、文档浅灰、配置暖棕、图片柔紫、音视频灰蓝、压缩包灰玫瑰、表格/数据灰橄榄，未识别类型使用中灰。识别忽略扩展名大小写，也支持 `Dockerfile`、`Makefile`、`README`、`.env`、`.zshrc` 等常见名称。文件符号链接保持柔紫色和 `↗` 标记；选中后仍保留类型色，背景为深灰。

选中文件时，右侧历史会话、工作台同步目录以及新建/恢复会话的 `-C` 参数都使用该文件所属的可见父目录，不会把文件路径当作 cwd。刷新会保留选中的文件；文件被删除后回退到最近可见的父目录。

对话区内的按键交给原版 CLI。macOS 功能键被系统占用时可用 `Fn+F6`，或用鼠标切区；Linux 桌面环境若拦截 `F6`，同样可用鼠标切区。也支持 tmux 默认的 `Ctrl-b` 后按 `o` 切区、`Ctrl-b` 后按 `d` 暂离。分界线可用鼠标拖动。

切换已打开的历史会话会保留各自进程，不会终止正在生成的回答。退出某个 CLI 后两侧栏仍保留，可以选别的会话或重新打开。暂离后，从同一目录再次运行 `zzb-codex-ui` 可回到对应工作台。暂离不是终止任务，正在运行的请求会继续执行；要结束请求请在对应对话中使用原版 CLI 的退出/中断操作。

## Summary

右侧上方显示所选目录（选中文件时为文件所属目录）的历史会话；下方 Summary 随列表选中项变化，显示数据库已有的模型、Token 用量、更新时间及内容预览。没有预览时显示首条请求并明确标注；缺失信息显示“未记录”。它不调用模型重新总结，也不把首条请求冒充完整对话总结。中央原版 status line 继续显示实时上下文和额度等信息。

左侧文件/目录选择通过独立 tmux 工作台状态同步到右侧，不写入 Codex 会话数据库。旧上下布局会在启动时迁移为三栏，保留正在运行的对话进程；只重新加载侧栏。

## 数据和隔离

目录匹配按真实路径精确比较，不合并子目录。只读状态目录下 `db/state_5.sqlite` 的会话元数据（默认 `~/.zzb-codex`，可用 `ZZB_CODEX_HOME` 或 `--state-dir` 指定浏览目录），排除归档项，不修改会话数据库。tmux 使用独立的 `zzb-codex-ui` socket 和空配置文件，不更改用户的 tmux 配置或默认 server。

```sh
zzb-codex-ui --list-json [目录]        # 非交互查询
zzb-codex-ui --state-dir /path/state  # 指定用于浏览的数据库目录
zzb-codex-ui --help
```

`--state-dir` 只影响浏览的数据源；真实对话仍由 zzb-codex 启动器管理（默认为 `$ZZB_CODEX_PREFIX/bin/zzb-codex`，可用 `ZZB_CODEX_LAUNCHER` 覆盖）。初始侧栏宽度随终端大小分配，中央通常占六成以上；可拖动分隔线调整，重连保留调整后的布局。建议使用较宽的终端，以完整展示原版 status line。

分屏与保留进程基于 [tmux 的 pane/session 机制](https://man.openbsd.org/tmux)。

## 验证

在本目录运行：

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

仅运行 UI 单元测试（不启动 tmux）：

```sh
python3 -m unittest discover -s . -p 'test_app.py' -q
```

集成测试使用独立临时 tmux server 和模拟对话，不启动真实模型请求。

## 文件预览

单击左侧文件（等待约 0.35 秒）或选中后按 `p` 打开预览；双击、`y`、文件上 Enter 继续复制路径。点击目录仍展开/折叠。

文本显示在占终端约 80% 的浮窗中，支持 ↑↓/jk、滚轮、PgUp/PgDn、g/G，以及 h/l 横向移动。按 q 或 Esc 关闭，回到原文件树。中央对话进程在预览期间继续运行。

图片在 macOS 使用系统 Quick Look 窗口；Linux 图形桌面使用 xdg-open 的默认图片查看器。SSH/无图形桌面会明确提示无法显示图片。图片不在终端浮窗内绘制，也不额外安装图像库。

文本最多读取 256 KiB，超出时显示截断提示；二进制文件显示大小说明。只读取普通文件，清除终端控制字符，不执行文件内容。独立非 tmux 模式的文本预览暂时占用当前终端，退出后返回浏览器。

新建会话或原生选择器中的会话获得真实 ID 后，工作台会根据进程打开的 writer-lock 文件路径识别正在运行的对话，复用原 pane；只检查文件路径，不读取会话正文。macOS 需要系统 lsof，Linux 使用 /proc。会话已在同一 tmux server 的另一个工作台运行时，提示切回原工作台并阻止重复启动。

历史列表优先显示原版 `/rename` 保存的 `name`；没有自定义名字时回退到 `title`，兼容旧数据库。后台每约 3 秒刷新，也可在侧栏按 r 立即刷新。
