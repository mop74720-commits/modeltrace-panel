# ModelTrace 上游指纹检测面板

按 [xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace) 的三次数字挑战、稳健 Hellinger 特征、有序块特征和校准 softmax 实现。指纹库固定于 commit `60949ef522a84f66b1236b459308b48028d36949`，当前 13 个候选模型、468 条参考指纹。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:BIND_HOST='127.0.0.1'; $env:PORT='7860'
python app.py
```

打开 `http://127.0.0.1:7860/`。自动模式支持 Chat Completions、Responses、Anthropic Messages；手动模式可复制挑战并粘贴完整输出。至少一份有效回答即可归因，三份结果使用原项目的三查询校准参数。

## Docker 部署

Linux VPS 的私有仓库拉取、HTTPS 反向代理、配置迁移及备份，见 [部署指南](deploy/VPS.md)。可复制 `.env.example` 为 `.env`，填写面板口令后运行 `docker compose up -d --build`。

```powershell
$env:PANEL_ACCESS_TOKEN='换成高熵随机口令'
$env:UPSTREAM_HOSTS='api.example.com'
$env:UPSTREAM_BASE_URL='https://api.example.com/v1'
$env:UPSTREAM_MODEL='你的模型名'
$env:UPSTREAM_API_KEY='你的密钥'
docker compose up -d --build
```

对外监听前必须配置 `PANEL_ACCESS_TOKEN`。默认禁止访问内网/环回地址；如果上游确实在内网，配置精确域名白名单并同时设置 `ALLOW_PRIVATE_UPSTREAM=1`。生产环境建议在反向代理启用 HTTPS、限速和访问日志脱敏。

## 设计约束

### 推理档位

Responses 使用 `reasoning: {"effort": "high"}`，Chat Completions 使用 `reasoning_effort: "high"`。下拉支持上游默认（不传参）、low、medium、high、xhigh、max、minimal、none；具体可用值依赖模型和上游。GPT-6 Astra 不支持 none。参考 [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)。

档位会随配置自动保存并在刷新后恢复；旧配置保持上游默认。报告保存的是请求档位，不能证明上游实际执行。Anthropic Messages 的 thinking 参数尚未接入，该协议禁用 OpenAI 档位选项。

- 上游信息填完整并停止输入约 1.2 秒后自动加密保存；点击检测也会先保存。保存成功才清空 Key 输入框，检测失败不影响配置。
- 刷新自动读取上次选择的配置（首次默认选择第一个）。修改模型、协议、预算时 Key 留空即可保留。更换 URL 需填写该地址的 Key。
- 配置保存在 `config/upstreams.enc`，本机解密密钥在 `config/.key`；两者需一起备份。这保护误读，不能防止已经掌握这两个文件的用户解密。Docker 使用持久卷保存该目录，也支持环境变量 `CONFIG_ENCRYPTION_KEY`。
- 配置保存不执行 DNS 解析和上游调用，实际检测时仍执行地址检查。列表不返回密钥，浏览器只保存上次选择的配置 ID，不保存 Key。
- 每轮最多三次请求，服务端并发槽位为 2，不自动重试，避免意外费用。
- 每个挑战由签名 token 绑定，24 小时过期；服务重启后旧 token 失效。
- Base URL 经过协议、DNS、IP 范围和可选域名白名单校验，防止开放代理式 SSRF。
- 结果是当前候选库内的闭集归因概率，不是能力评分，也不能直接解释成“降智百分比”。

## 来源与许可

算法和参考指纹库来自上游仓库并保留其 MIT License 文本，见 `vendor/LICENSE` 和 `vendor/UPSTREAM_README.md`。
