# 小小鵺

Windows 上的 OneBot 群聊机器人与本机控制面板。当前版本 **1.1.1**。

支持人设编辑、短句与表情、上下文和调用额度控制、多个群并行运行、天网管理、群专属插件、可恢复卸载、回复文案编辑，以及按每次机器人发言前后各10条消息提炼的群专属学习日志。模型服务使用兼容 Chat Completions 的 HTTPS API。

## 本机部署

需要 Python 3.11 或更新版本，以及你自行安装、配置并登录的 QQ/OneBot 接入端。本仓库不附带 QQ、SnowLuma、第三方运行时、插件图片或任何账号信息，也不自动注入或安装 QQ 插件。

在程序目录中执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_local.py
```

初始化只创建缺少的本地文件，不覆盖已有配置。填写本机 `model.json` 的API地址、模型与密钥，确认 `account.json` 中的机器人、主人、默认群号和 OneBot 配置路径正确，然后运行 `start-panel.ps1`。

面板地址：`http://127.0.0.1:5100/`。首次配置完成后，在面板选择参与群，再启动机器人。默认不开启天网处罚、插件或记忆学习；开启某群学习前应确认该群消息可送往你选用的模型服务。机器人只使用配置的主人账号接收停机指令。

## 从已有小小鵺迁移

首次迁移需要私下复制你自己的配置和资源，并运行 `setup_local.py` 补充账号文件。各电脑独立保存 `account.json`、`model.json`、`settings.json`、`persona.txt`、`moderation.json`、表情资源、插件素材和学习日志；这些内容禁止提交到公开仓库。公开程序包不包含媒体资源，抽签、塔罗、食物、原曲和符卡数据需从你自己的旧部署迁移。

机器人主体更新时先停止所有群后台并退出面板服务，备份程序和本地数据，再将新包的程序文件复制到原目录。发布包不会包含上面的私有文件；不要删除这些本地数据。启动面板核对版本后，由你决定恢复机器人。

## 自动检查更新

在“07 备份与工具”填写发布仓库 `main` 分支下 `latest.json` 的原始内容地址：

`https://raw.githubusercontent.com/YanagawaHsk/nuebot/main/latest.json`

v1.1.2 起，如果原始文件域名访问超时，可以改用 GitHub 官方 API 地址：

`https://api.github.com/repos/YanagawaHsk/nuebot/contents/latest.json`

也可使用相同清单的公开镜像：

`https://cdn.jsdelivr.net/gh/YanagawaHsk/nuebot@main/latest.json`

镜像有缓存，新发布的版本可能延迟显示；能直接访问 GitHub 时优先使用原始地址。

两端填写同一地址。默认每6小时检查一次，也可立即检查；显示新版本和下载地址。当前功能检查和提示更新，不自动安装，不同步你的密钥、人设、群配置、账号登录状态或学习日志。

## 发布新版本

修改 `version.json` 的版本号和发布日期，以及 `CHANGELOG.md`。提交到自己的仓库后，推送与版本号相同的标签，例如 `v1.1.2`。

GitHub Actions 会用文件白名单生成 Windows 源码包，计算 SHA256，发布 Release，再更新 `main` 分支的 `latest.json`。包中的版本必须与标签一致。未来新增程序文件时同步修改 `tools/build_release.py` 的白名单。

首次发布可在工作流中选择手动运行，输入当前标签；这会发布或补全同名 Release。更新清单只在程序包上传成功后更新。

## 管理与边界

面板保持只监听本机。远程管理需要另外设置可信的私有隧道；不要直接把此面板开放到公网。运行主机的管理员仍可修改程序或读取本地密钥，主人指令不构成对主机管理员的技术隔离。

本仓库包含程序适配层，不发布第三方插件图片、用户表情或私聊、群聊记录。首次迁移资源请自行确认其使用范围。
