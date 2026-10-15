# linuxstudio-practice

Windows 上可安装、可分发的 Codex Skill。Agent 读取 LinuxStudio 实际练习题并生成答案，Python HTTP 提交每一关，最后核验同一账号的进度点亮。无需浏览器前台、截图或模拟鼠标。

范围：`www.linuxstudio.cn` 的 **Linux常用命令** 练习；不支持其他章节或考试。工具不接入模型，也不会独立生成答案，需由能运行本地 Python 的 Agent 按 Skill 工作流调用。

## 安装

需要 Windows 10/11、Python 3.10+（含 Tk）和网络。解压安装包或克隆仓库后，在 PowerShell 运行：

```powershell
python install.py
```

也可双击 `install.cmd`。安装器校验 `MANIFEST.json`，安装到 `$HOME\.agents\skills\linuxstudio-practice`，创建独立 `.venv` 并安装 requests、beautifulsoup4。依赖下载需联网；安装不登录、不提交题目。Codex 未发现新 Skill 时重启应用。

高级：`python install.py --skills-dir D:\AgentSkills`。已有依赖的测试环境可加 `--no-deps`，随后调用者 Python 必须已安装依赖。更新运行 `python install.py --upgrade`：旧版本保留为同目录隐藏备份，个人数据位置不变。确认新版本可用后可手动清理备份。

其他 Agent 可直接调用 `linuxstudio-practice\run.cmd`；Skill 不依赖 Codex 专属工具。首次使用请阅读 `linuxstudio-practice\SKILL.md`。

## 首次登录与使用

```powershell
$skill = Join-Path $HOME '.agents\skills\linuxstudio-practice'
& "$skill\run.cmd" doctor
& "$skill\run.cmd" login
```

在本机窗口输入密码。可选保存，默认不勾选；仅网站确认登录成功才保存。保留“登录会话”窗口，关闭按钮只会最小化。后续 HTTP 任务复用登录，不弹框。“重新登录 / 切换账号”可选新账号或已保存账号；“退出登录”使旧后台任务失去继续请求的权限。网站使会话过期时仍需重登。

可以直接给 Agent：

> 使用 $linuxstudio-practice，复用当前登录，完成 mkdir 的全部关卡，确认账号进度点亮并记录耗时。不占用浏览器前台；没有可复用会话时先告知我。

调用流程：`catalog → job start → job pending → 常规题直接作答 → job submit → 完成或跳过卡住的命令 → job report`。不再预读全部教程。不熟悉的命令或首次判题失败时，才用 `lesson` 查该命令的相关部分，随后 `job attach-lessons` 附加获取凭据；同一命令后续关卡复用已读内容。整个授权清单放在一个作业，数量由程序统计。

默认 **每个命令累计失败 3 次，就跳过该命令并继续其余项目**，不再因普通判题拒绝而整批停止。全部处理后，有跳过项则状态为 `partial`，最终集中报告卡关清单；不自动反复补做。教程无法获取等具体困难也可用 `job skip` 显式记录后继续。账号失效、提交超时结果未知、坏答案 JSON 或并发冲突仍会停止整批，且不自动重试 POST。仅这类执行异常需要人工审核恢复。`job report --verify-progress` 核验实际完成的部分，保留拒绝、跳过、教程凭据与失败历史。完整命令和报告格式见 [CLI 参考](linuxstudio-practice/references/cli.md)。

## 运行数据

安装包和仓库只含源码、Skill、安装器、文档和合成数据测试，不含真实账号密码、会话、个人练习记录。默认数据位置 `%LOCALAPPDATA%\LinuxStudioPractice`：

- `private`：当前 Windows 用户 DPAPI 加密的账号密码和本工具自己的 HTTP Cookies；不读取 Chrome 数据。
- `jobs`：本地个人报告，含账号、题目、答案、反馈、耗时，请勿上传。

使用 `run.cmd --data-dir D:\PrivatePractice session status` 可更换数据根目录，所有参与进程必须同目录。不同目录的会话相互隔离。升级和卸载 Skill 不删除数据。移除保存密码在登录窗口操作；退出登录不等于移除保存账号。

网站当前为明文 HTTP；DPAPI 只保护落盘数据，不能加密网站传输。只对本人或已获授权的账号使用。工具不会伪造关卡或进度。POST 超时视为结果未知并停止，避免盲目重复提交。

## 测试与开发

```powershell
python -m pip install -r linuxstudio-practice/requirements.txt
python -m unittest discover -s tests -v
python tools/build.py
```

离线测试使用合成账号、Cookie 和网站响应，覆盖加密保存、账号切换、旧任务失效、静默复用、逐关提交和进度核验。Tk 测试只创建自动关闭的隐藏窗口，不读取浏览器或真实密码。离线测试不等于任意账号实时验收；真实操作应在本机登录后以 `job verify` 为准。

`tools/build.py` 更新清单并以明确文件范围打包，禁止包含凭据、会话和个人记录。ZIP 默认在仓库外 `dist`，可显式指定目录。源码修改后须先重建清单再安装。清单用于完整性校验，不是作者签名。

版本：1.2.0。改为按需查教程、单命令阈值跳过并继续、`partial` 结果和手动跳过入口。`job start` 不再强制教程凭据；拒绝后重试才要求通过 `job attach-lessons` 附加相关凭据。保留 Windows 文件占用重试和关键执行异常的停机保护。
