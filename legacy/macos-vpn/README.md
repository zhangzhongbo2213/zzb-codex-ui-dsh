# 原 macOS 独立代理实现

这里保存已有部署的可读源码，不由主安装器自动执行：

- `supervisor.py` / `core_rpc.py`：监督 iKuuuVPNCore，检查固定节点，失败后重启。
- `refresh.py` / `read-vpn-config.swift`：从本机 iKuuu 应用保存的订阅更新指定节点。
- `zzb-codex-vpn.py`：原服务的 status/start/stop/restart/check/refresh 管理命令。
- `com.zzb.codex-vpn.plist.template`：launchd 用户服务，`__HOME__` 需替换为目标用户主目录。

原布局为 `~/local/share/zzb-codex-vpn/`，包含 `state/config.json`、`logs/` 和专用 `iKuuuVPNCore`。私有节点配置、控制器口令及核心二进制未收录。恢复时需要自己的兼容 macOS 核心和订阅，并建立该目录布局、安装 launchd 模板，再将管理脚本放到 `~/local/bin/zzb-codex-vpn`。

这份实现使用 macOS Keychain、Swift 和 launchd。Linux 无法执行该部署方式；主项目跨平台启动器支持使用 Linux 上已有的本地 HTTP 代理或兼容控制器，配置方法见根 README。
