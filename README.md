# zzb-codex 可迁移配置

保存这套独立 Codex 终端工作台的源码、偏好和安装流程。左侧文件树，中央原版对话与 status line，右侧历史会话与 Summary；包含 Vim 风格按键、鼠标操作、低饱和深色主题和文件类型配色。

支持 macOS Apple 芯片 / Intel，以及 Linux ARM64 / x86-64。安装器根据机器选择锁定的 **Codex 0.166.0 完整运行包**，校验 SHA-256 后安装，包含 code-mode host、exec server 和运行资源。下载地址与校验值见 [runtime-lock.json](config/runtime-lock.json)。

## 新机器安装

需要 Python **3.12+**、tmux 和 Git。安装命令不需要 sudo，也不会修改全局 `codex`、系统代理或 shell 配置。

macOS：

```sh
brew install python tmux git
```

Ubuntu 24.04 / Debian 等环境先确保 Python 版本达到 3.12，再安装终端依赖：

```sh
python3 --version
sudo apt-get install tmux git
# Wayland 桌面使用 wl-clipboard；X11 桌面使用 xclip 或 xsel
sudo apt-get install wl-clipboard xclip
```

复制本仓库（含 `.git`），或使用随附 Git bundle：

```sh
git clone /path/to/zzb-codex-setup.bundle zzb-codex-setup
cd zzb-codex-setup
python3 scripts/install.py --plan
```

先准备一个可用的本地 HTTP 代理。使用已有代理的安装方式如下，端口替换为目标机器实际端口：

```sh
python3 scripts/install.py --proxy-url http://127.0.0.1:17890
export PATH="$HOME/local/bin:$PATH"
python3 scripts/doctor.py --check-proxy
zzb-codex --version
zzb-codex login --device-auth
zzb-codex-ui resume
```

`--proxy-url` 使用已有代理并检查其监听端点；代理不可用时拒绝新的联网会话，不自动直连。这种模式不校验代理出口国家或节点。要恢复原先固定日本节点的控制器校验，使用下方“代理与独立状态”中的严格模式。

需要持久保留 PATH 时，将上面的 `export PATH=...` 加到自己的 `.zshrc` 或 `.bashrc`。无图形显示服务的 Linux/SSH 会话仍可浏览和对话；系统剪贴板复制需要可用的 Wayland/X11 会话及相应工具。

若网络无法下载官方安装包，可以在另一处下载锁定包后离线安装；校验步骤仍会执行：

```sh
python3 scripts/install.py --archive /path/to/codex-package-<target>.tar.gz --proxy-url http://127.0.0.1:17890
```

## 代理与独立状态

默认程序位置是 `~/local`，独立状态是 `~/.zzb-codex`。安装器生成 `config.toml` 和仅本机保存的 `local-settings.json`；凭据、数据库、会话和日志由原版 Codex 在独立目录生成。启动器清除继承的 Codex/OpenAI 身份环境变量，固定独立存储和代理设置，保留原来的隔离方式。

严格模式要求本地 Clash 兼容控制器 `127.0.0.1:17891`、代理端口 `17890`、唯一 MATCH 规则指向 `🇯🇵 日本S02 | IEPL`。先在目标机器配置自己的代理服务，再提供其私有 JSON 配置：

```sh
python3 scripts/install.py --proxy-config /private/path/proxy-config.json
```

`config/proxy.example.json` 只有结构和占位符，没有节点凭据，不能直接联网使用。节点与控制器口令需在新机器单独配置。安装器不会安装或启动 VPN 服务，也不会把私有配置放进 Git。原机器的 iKuuu/macOS 常驻代理源代码及服务模板保存在 [legacy/macos-vpn](legacy/macos-vpn/README.md)；其专用核心与 Keychain 读取不适用于 Linux，Linux 使用自己的兼容本地代理。

不用代理参数时，安装器默认生成严格模式配置；在原安装上更新时会识别已有 `~/local/share/zzb-codex-vpn/state/config.json`。配置已存在时更新不会改写它；以后调整代理请编辑独立状态目录中的 `local-settings.json`。

## dsh-delegate

技能和 stdio MCP 桥接代码随仓库保存。新机器需要自行安装 Node.js、dsh CLI 并配置 dsh 的模型服务；这些账户配置不在仓库中。

首次安装且 dsh/Node 已可用时，可直接注册 MCP：

```sh
python3 scripts/install.py --with-dsh --proxy-url http://127.0.0.1:17890
```

已有配置保持原样。后续手动注册可使用：

```sh
zzb-codex mcp add dsh -- python3 "$HOME/local/share/zzb-codex-setup/dsh-bridge/dsh_mcp.py"
```

在 `~/.zzb-codex/config.toml` 的 `[mcp_servers.dsh]` 中设置 `tool_timeout_sec = 660`；确保 `dsh` 和 `node` 在启动 zzb-codex 时的 PATH 中。新会话可用 `$dsh-delegate` 调用技能。首次安装的 `--with-dsh` 会自动生成这些设置。

### 技能存在，但提示“没有注册 dsh MCP”

技能文件和 MCP 服务注册是两件事。`zzb-codex` 使用独立的 `~/.zzb-codex/config.toml`，不能只在普通 Codex 的 `~/.codex/config.toml` 中注册 dsh。项目配置可能让主目录中的会话恰好能使用 dsh，而换到另一个目录就找不到。应在实际工作的项目目录检查：

```sh
zzb-codex mcp list
```

如果没有 dsh，首次安装使用上面的 `--with-dsh`；已有安装使用上面的 `zzb-codex mcp add dsh ...` 注册，并检查独立配置文件中的启动命令、PATH 和超时。不要将普通 Codex 的整个配置文件覆盖到独立目录。

配置完成后，在报错的中央对话中用 `/quit` 结束原进程，再从右侧历史列表重新打开该会话。输入 `/mcp` 应显示 `dsh: connected (3 tools)`；`/mcp verbose` 可查看 `dsh_run`、`dsh_resume`、`dsh_bridge_info`。导航区按 `q` 仅暂离，后台进程继续运行，不等于重新加载 MCP。官方操作说明见 [MCP 文档](https://developers.openai.com/codex/mcp)。

## 自定义位置和更新

```sh
python3 scripts/install.py --prefix "$HOME/apps/zzb" --state-dir "$HOME/.zzb-work" --proxy-url http://127.0.0.1:17890
python3 scripts/doctor.py --prefix "$HOME/apps/zzb" --state-dir "$HOME/.zzb-work"
```

之后编辑仓库中的文件并提交，再重新安装：

```sh
git add ui config scripts launchers docs
git commit -m "Update workspace configuration"
python3 scripts/install.py --update
```

`--update` 更新程序和技能，备份被替换文件，并保留现有 `config.toml`、`local-settings.json` 和会话。自定义安装位置需要在更新时再次传入相同的 `--prefix` / `--state-dir`。`--skip-runtime` 只安装 UI 和配置，供开发或已有运行包的场景使用。

UI 可用 `ZZB_CODEX_HOME`、`ZZB_CODEX_PREFIX`、`ZZB_CODEX_LAUNCHER`、`ZZB_TMUX` 覆盖路径。详细操作见 [UI 说明](ui/README.md)。账号可用模型取决于新机器上登录的账号；默认模型和推理强度保存在 `config/base.toml`，可自行修改。

## 内容和验证

- `ui/`：完整 UI 源码和原有回归测试。
- `launchers/`、`scripts/`：独立启动器、安装器、环境检查和 Git 文件检查。
- `config/`：模型、原版 status line、运行包版本锁及无凭据代理模板。
- `skills/`、`vendor/dsh-bridge/`：dsh 技能与桥接代码。
- `legacy/macos-vpn/`：原 macOS 代理实现的源码快照。

```sh
python3 -m unittest discover -s ui -p 'test_*.py' -v
python3 -m unittest discover -s tests -v
python3 scripts/check_repo.py
```

GitHub Actions 已配置 macOS/Linux 测试矩阵，推送到 GitHub 后才会运行。本机验证结果见 [验证记录](docs/verification.md)。测试使用临时目录和独立 tmux server，不调用真实模型。仓库不包含认证信息、会话记录、代理订阅、运行包二进制、缓存或日志。

来源：[原版 Codex CLI 文档](https://developers.openai.com/codex/cli)、[锁定版本发布页](https://github.com/openai/codex/releases/tag/rust-v0.166.0)。dsh 来源记录见 [SOURCE.md](vendor/dsh-bridge/SOURCE.md)。
