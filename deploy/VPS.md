# Linux VPS 部署

适用于已安装 Docker Engine 和 Docker Compose 插件的 Linux VPS。查看 [Docker 官方安装文档](https://docs.docker.com/engine/install/)；用 `docker version` 和 `docker compose version` 确认已可用。以下命令使用 Bash。

## 1. 拉取私有仓库

在 VPS 上使用有仓库读取权限的 GitHub SSH key（推荐只读 deploy key），或使用已登录的 `gh repo clone`。不要把 GitHub token 拼进 clone URL。

```bash
git clone git@github.com:mop74720-commits/modeltrace-panel.git
cd modeltrace-panel
cp .env.example .env
chmod 600 .env
nano .env
```

在 `.env` 中设置高熵的 `PANEL_ACCESS_TOKEN`。其他项可留空，上游 URL、Key、模型和档位直接在面板中填写并自动保存。不要将真实 `.env` 提交到 GitHub。

正常 VPS 公网 DNS 下保留 `ALLOW_PRIVATE_UPSTREAM=0`；本机代理的保留地址绕行设置未写入部署配置。`UPSTREAM_HOSTS` 可以填写允许的上游域名，以逗号分隔。

## 2. 构建并启动

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:7860/healthz
```

健康接口应返回 `{"status":"ok"}`。出错时查看 `docker compose logs --tail=100 modeltrace`。

容器使用非 root 用户、只读根文件系统，配置目录通过 `modeltrace_config` 命名卷持久化。升级或重建容器会保留配置。镜像创建了 `/app/config` 并设置应用用户权限，首次命名卷会继承该目录权限。

## 3. 访问页面

服务默认只绑定 VPS 的 `127.0.0.1:7860`。

临时使用：在你的电脑运行 SSH 隧道，然后打开本机 `http://127.0.0.1:17860`。

```bash
ssh -N -L 17860:127.0.0.1:7860 用户名@VPS地址
```

正式使用：将域名解析到 VPS，在已有 Nginx / 宝塔 / 1Panel 中为此域名配置 HTTPS，把请求反向代理到 `http://127.0.0.1:7860`。参考同目录 `nginx.conf.example` 的 location 配置。保留原始 Host，读取超时至少 300 秒；单个上游请求最长等待 240 秒。若前面还有 CDN，也需注意其请求超时。

打开页面后输入 `.env` 中的面板访问口令。此口令允许使用所有已保存上游，请仅供可信管理员使用。面板接口不记录请求正文；反向代理也不应记录正文或鉴权头。

## 4. 从本机迁移已保存上游

GitHub 仓库不包含你的 Key。若本机已保存配置，通过 SSH/SFTP 将整个本机 `config/` 目录（包括隐藏文件 `.key`）传到 VPS 仓库的 `config/` 下。不要在聊天中粘贴文件内容。以下操作用于新建、尚未录入配置的 VPS 实例。

```bash
docker compose create
docker compose cp config/. modeltrace:/app/config/
docker compose run --rm --no-deps --user root modeltrace \
  sh -c 'chown -R panel:panel /app/config && chmod 700 /app/config && chmod 600 /app/config/* /app/config/.key'
docker compose up -d
```

如果原机器通过 `CONFIG_ENCRYPTION_KEY` 提供密钥，目标 VPS 也必须设置相同值；不要生成新值替代。已有 VPS 配置时先备份，不能直接覆盖。仅克隆代码不会迁移上游配置。

## 5. 更新和备份

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
```

备份整个配置目录（包含隐藏 `.key`）：

```bash
mkdir -m 700 -p backups
docker compose cp modeltrace:/app/config/. backups/
chmod -R go-rwx backups
```

备份包含解密所需材料，应保存在受保护的位置；如用环境变量密钥，还需单独保管 `.env`。不要执行 `docker compose down -v`，其中 `-v` 会删除持久卷。

## 当前功能边界

- 已实现多上游配置保存、单上游三次挑战、推理档位、手动粘贴和 JSON 报告导出。
- 尚未实现批量并行检测和测试历史自动保存；测试结果刷新会丢失，需要先下载 JSON。
- ModelTrace 归因概率不是降智百分比，也不能证明上游的真实模型身份。
