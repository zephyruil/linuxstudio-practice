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

调用流程：`catalog → lesson（读取本站教程并保存凭据 ID）→ job start --lesson-receipts <ID> → job pending → 生成答案 → job submit → 全部关卡 → job verify → job report`。每个命令先读一次本站教程，首次被拒绝后对照教程和完整题目再修改答案。整个授权清单放在同一个作业，不分派给多个 Agent；数量由程序统计。

默认整个作业累计 **3 次判题失败即停**，通过一关不清零。工具异常、无效答案文件或并发冲突立即停止。停止后禁止自动重开绕过预算；新作业会被阻止。只有用户明确要求重试，才运行 `job acknowledge`，由用户在本机窗口确认审核；失败记录保留。`job report --verify-progress` 可在停止后只读核验已完成的部分，并保留所有拒绝、教程获取凭据及停止原因。完整命令和最终报告格式见 [CLI 参考](linuxstudio-practice/references/cli.md)。

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

版本：1.1.0。新增累计错误阈值、停止后人工审核、教程读取凭据和完整最终报告。调用变更：`job start` 必须带 `--lesson-receipts`，不能沿用 1.0.x 的无凭据启动方式。保留 Windows 文件占用的本地重试，不重试 HTTP 提交。
