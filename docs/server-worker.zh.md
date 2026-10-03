# BioDSH 一对多计算服务器接入与资源监控

## 连接模型

桌面端只配置一个 Platform 地址并登录。Platform 保存服务器登记、节点状态和资源历史；每台计算服务器运行一个 `biodsh-worker`，主动向 Platform 发起 HTTPS 请求。桌面端不会保存服务器 SSH 密码，也不要求每台服务器开放额外的入站端口。

```mermaid
flowchart LR
    D[BioDSH Desktop] -->|用户令牌 / HTTPS| P[Platform API]
    W1[Worker · Server 01] -->|节点凭据 / 心跳与指标| P
    W2[Worker · Server 02] -->|节点凭据 / 心跳与指标| P
    P --> DB[(PostgreSQL)]
    P --> D
```

增加第二台或更多服务器时，只需在每台服务器重复安装与注册步骤并使用不同节点名。桌面端会从 `/api/v1/nodes` 自动得到完整节点列表。

## 节点注册与凭据

1. Worker 首次注册使用平台初始化令牌调用 `/api/v1/nodes/register`。
2. Platform 生成该节点独享的高熵令牌，只返回一次，数据库只保存 SHA-256 摘要。
3. Worker 将节点 ID 和节点令牌写入权限为 `0600` 的凭据文件。
4. 后续心跳只能使用节点 ID 与节点令牌。初始化令牌从 Worker 环境中删除。
5. 管理员可以轮换节点令牌；旧令牌立即失效。

不允许多台服务器复制同一份 Worker 凭据。每台服务器必须独立注册。

## Linux 安装

在服务器创建独立系统用户和运行目录，将本仓库或发布包中的 `platform` 安装进虚拟环境：

```bash
sudo useradd --system --home /var/lib/biodsh --create-home biodsh
sudo mkdir -p /opt/biodsh-worker /etc/biodsh /var/lib/biodsh
sudo python3 -m venv /opt/biodsh-worker/.venv
sudo /opt/biodsh-worker/.venv/bin/pip install /path/to/BioDSH/platform
sudo cp deploy/linux/worker.env.example /etc/biodsh/worker.env
sudo cp deploy/linux/executors.example.json /etc/biodsh/executors.json
sudo chown root:biodsh /etc/biodsh/worker.env
sudo chmod 640 /etc/biodsh/worker.env
sudo chown -R biodsh:biodsh /var/lib/biodsh
```

编辑 `/etc/biodsh/worker.env`，配置 Platform HTTPS 地址、唯一节点名、指标磁盘路径和初始化令牌。然后执行一次注册：

```bash
sudo --user biodsh bash -c \
  'set -a; source /etc/biodsh/worker.env; exec /opt/biodsh-worker/.venv/bin/biodsh-worker enroll'
```

注册成功后从 `worker.env` 删除 `BIODSH_WORKER_BOOTSTRAP_TOKEN`，再验证一次心跳：

```bash
sudo --user biodsh bash -c \
  'set -a; source /etc/biodsh/worker.env; exec /opt/biodsh-worker/.venv/bin/biodsh-worker run --once'
```

最后安装并启动 systemd 服务：

```bash
sudo cp deploy/linux/biodsh-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now biodsh-worker
sudo systemctl status biodsh-worker
```

生产环境应使用正式软件包安装流程替代仓库路径，并由部署系统注入环境文件。服务模板默认限制系统写权限，仅允许写入 `/var/lib/biodsh`。

## 任务执行器

`/etc/biodsh/executors.json` 是服务器本地的任务类型白名单。每个任务类型配置固定命令、固定环境变量、最长运行时间、最大参数数量和结果文件匹配规则。新增生信流程时增加执行器配置或部署新的流程包，不修改桌面端，也不把服务器路径写进数据库。

Worker 使用参数数组直接创建子进程，不调用 Shell。每次运行使用 `/var/lib/biodsh/jobs/<任务ID>/attempt-<次数>` 独立目录，清理继承环境，并创建独立进程组。取消任务时先发送终止信号，超过宽限期后强制结束整个进程组。

任务处理顺序如下：

1. 桌面端把任务类型、参数数组和 CPU、内存、磁盘、GPU 需求提交给 Platform。
2. Worker 上报本机执行器类型和实时资源，然后主动领取可运行的最高优先级任务。
3. Platform 使用条件更新完成原子领取，并签发短期执行租约；资源不足的节点不会领取任务。
4. Worker 运行期间续租、上报资源并把标准输出写入顺序化事件流。桌面端每两秒刷新日志。
5. 用户取消后，Platform 把任务置为取消中；Worker 在下一次控制轮询中终止进程。
6. Worker 按 `result_globs` 流式上传结果。Platform 校验大小、计算 SHA-256 并保存产物记录。
7. 桌面端列出产物，并通过用户权限认证下载到本地 `BioDSH/results/<任务ID>`。

如果领取后尚未启动便失去租约，任务会重新排队；如果运行中的任务失去租约，Platform 将其标记为 `unknown`，防止自动重复执行产生两份结果。管理员确认远端进程状态后再决定是否重试。

## 资源可视化数据

Worker 默认每 10 秒采集并上报：

- CPU 使用率和 1 分钟负载；
- 内存已用量、总量和百分比；
- 指定数据磁盘的已用量、总量和百分比；
- 网络累计接收与发送字节；
- NVIDIA GPU 型号、利用率、显存和温度；
- 当前运行任务数。

Platform 将数据写入 `node_metrics` 时序表，默认保留 7 天。节点超过 45 秒未上报时，读取接口会将其判定为离线。两个阈值都通过环境变量配置。

桌面端“计算服务器”页面每 10 秒刷新当前指标，并读取最近一小时历史绘制 CPU、内存和磁盘曲线。管理员还可以把节点设置为排空、禁用或重新启用。排空状态将在任务调度阶段用于停止分配新任务，同时允许已有任务结束。

## 当前边界与下一步

服务器连接、资源监控、任务领取、资源匹配、受控子进程、实时日志、取消、执行租约、结果上传和桌面下载已经进入代码。执行器模板只是接入协议示例；正式上线前仍需为每种生信流程提供版本化的输入 Schema、容器或锁定环境、小数据集验收和失败恢复测试。
