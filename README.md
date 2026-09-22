# Backup To Nas

Backup To Nas 是一个基于MCDReforged的异地备份插件

使用SFTP协议将完整的zip格式备份传输到远端服务器(nas)

## 功能

- 按配置备份一个或多个世界并自动上传。
- 定时自动备份。
- 手动上传临时目录中已有的 `.zip` 文件。
- 权限等级和 SFTP 连接参数均可配置。

## 安装

插件要求 MCDR 2.10.0 或更高版本，并依赖 Paramiko：

```bash
pip install -r requirements.txt
```

如果 MCDR 通过 pipx 安装，请将 Paramiko 安装到运行 MCDR 的环境中：

```bash
pipx inject mcdreforged paramiko
```

插件首次加载时会生成：

```text
config/backup_to_nas/config.json
```

## 配置

下面是完整配置示例：

```json
{
    "permissions": {
        "help": 0,
        "make": 3,
        "interval": 3,
        "status": 0,
        "upload": 3
    },
    "interval": "",
    "temp": "./backup_to_nas",
    "turn_off_auto_save": true,
    "server_path": "./server",
    "world_names": [
        "world"
    ],
    "ignore_session_lock": true,
    "sftp": {
        "host": "192.168.1.100",
        "port": 22,
        "username": "root",
        "password_file": "/etc/mcdr/backup_to_nas/sftp_password",
        "private_key_file": "",
        "remote_dir": "/minecraft-backups",
        "timeout": 30,
        "auto_add_host_key": false
    }
}
```

`server_path`、`temp` 和 `world_names` 用来确定本地存档位置。

相对路径相对于 MCDR 的工作目录，建议在生产环境使用绝对路径。

`world_names` 中可以填写多个世界目录。

SFTP 认证方式二选一：

- `password_file` 指向只包含密码的文件，文件末尾的换行会被忽略。
- `private_key_file` 指向 SSH 私钥文件。

两项不能同时配置,文件认证的方式是出于安全性考虑。

`auto_add_host_key` 默认为 `false`，此时需要手动添加know_hosts；

嫌手动添加麻烦且明确接受该风险时才设置为 `true`。

`interval` 为空表示关闭自动备份，也可以使用 `1s`、`30s`、`1h` 或 `1d` 等格式。修改配置后重载插件即可生效。

## 命令

默认命令前缀是 `!!btn`：

| 命令 | 作用 |
| --- | --- |
| `!!btn` | 显示帮助信息 |
| `!!btn make` | 立即创建并上传一次备份 |
| `!!btn interval <interval>` | 设置自动备份间隔，例如 `!!btn interval 6h` |
| `!!btn interval off` | 禁用自动备份 |
| `!!btn status` | 查询当前或最近一次 SFTP 上传状态 |
| `!!btn upload` | 上传临时目录顶层已有的 `.zip` 文件 |

备份和手动上传不能同时执行。上传失败的 ZIP 文件会保留在临时目录中，可以稍后使用 `!!btn upload` 重试。

## 许可证

本项目使用 LGPL-3.0，详见 [LICENSE](LICENSE)。
