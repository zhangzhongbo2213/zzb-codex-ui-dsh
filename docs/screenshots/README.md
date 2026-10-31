# 终端界面捕获

- `workspace.png`：真实 UI 三栏布局，中央为原版 `zzb-codex -h` 离线输出。
- `keyboard-help.png`：在左侧面板按 `?` 后的真实帮助界面。

使用独立 tmux server、临时文件树和示例 SQLite 会话元数据，通过 `capture-pane -p -e` 捕获 ANSI 输出，再用 pyte / Pillow 渲染为 PNG。它们是终端内容的渲染截图，不是桌面截屏，也不是实际模型对话。未读取真实会话或登录凭据；演示服务捕获后已关闭。实际使用时中央运行原版交互对话和 status line。
