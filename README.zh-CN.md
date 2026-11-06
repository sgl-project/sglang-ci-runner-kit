# sglang CI runner kit

[English](README.md)

sglang 的厂商 CI 跑在厂商自己的硬件上。厂商机器不持有任何 GitHub token：每跑一个
job，机器向 sglang 的 runner 签发服务（broker）申请一份**即时（JIT）runner 配置**。
这份配置在该厂商的 runner group 里注册一个 runner，跑完一个 job 后这个 runner 自动注销。

厂商需要的东西都在这个 repo 里：

| 路径 | 内容 |
|---|---|
| `client/jit_client.py` | 生成主机密钥对；签名请求，领取一份 JIT 配置 |
| `client/runner_loop.sh` | 每台主机上的循环：领配置 → 跑一个 job → 清理 → 再领 |
| `examples/systemd/` | 两种模式的 systemd unit 和 env 文件示例 |
| `keys/<vendor>/<kid>.pub` | 已登记的各厂商主机公钥 |
| `vendors/<vendor>/submitters.txt` | 可以替该厂商提交公钥的 GitHub 账号 |

## 工作方式

1. 每台主机有自己的 Ed25519 密钥对。私钥留在主机上，公钥通过 PR 提交到这里。
2. sglang review 并合入 PR，再经过一次单独 review 的变更把公钥同步进 broker 并部署。
   部署完成后这把 key 才能用。
3. 主机用私钥签一个短期 token（60 秒），请求 `<BROKER_URL>/v1/jit/<vendor>`。broker 检查 key、主机名、
   labels、限流，以及这台主机已有的 runner 数，返回一个 runner 的配置。
4. runner 跑完一个 job 就消失，循环再去领下一个。

每把 key 能做什么由 broker 决定：labels、主机名、这台主机同时能有多少个 runner、限流、何时过期。
sglang 可以对某一把 key 单独收紧。请求或 token 里的任何内容都无法放宽。

## 部署要求

不管用什么来跑 runner（systemd、Kubernetes、Slurm、Jenkins），都要满足这四条：

1. **私钥放在不受信代码碰不到的地方**：放在一台不跑 CI job 的 provisioner 上。
2. **一个 job 一份配置，在全新、隔离的环境里跑**。runner 目录和容器都不能跨 job 复用。
3. **PR 代码绝不在私钥所在的地方运行**。CI 会执行公开 PR 里的代码，要当作恶意代码对待。
4. **失败后带随机抖动地退避**。broker 的请求额度由所有厂商共享。`runner_loop.sh` 已经实现了这一点。

`runner_loop.sh` 实现了下面两种常见部署方式。其他方式只要满足这四条也可以。例如在 **Slurm** 上：
- 私钥不要放在计算节点能看到的共享 NFS 或 `/home` 上；
- JIT 配置用 `0600` 文件传给 job，不要用 `sbatch --export`（`scontrol show job` 能看到）；
- 节点会被复用，所以每个 job 都要在新的 enroot/pyxis 或 Apptainer 容器里跑。

## 接入流程

### 1. 登记 GitHub 账号

告诉 sglang 哪些 GitHub 账号会替贵司提交公钥。sglang 会在 GitHub 之外和你们确认一次，
然后把这些账号加进 `vendors/<vendor>/submitters.txt`。此后所有密钥变更都通过本 repo 的
PR 完成，不要再通过聊天或邮件发送密钥文件。

### 2. 在每台主机上生成密钥

```bash
pip install -r client/requirements.txt
```

```bash
python3 client/jit_client.py keygen --out /etc/sglang-ci/<kid>
```

会生成 `<kid>.key`（权限 0600，留在主机上）和 `<kid>.pub`。要在主机本机上生成，
一台主机一对，不要在主机之间复制。

**kid 怎么起。** 格式是 `<vendor>-<host>-<yyyy>-<mm>`，最长 64 个字符，例如 `mlu-host01-2026-11`：
- `<host>` 是这把 key 可以注册 runner 的主机名（`[A-Za-z0-9][A-Za-z0-9.-]{0,39}`，不能有下划线）。
  `RUNNER_HOST` 必须和它完全一致。
- `<yyyy>-<mm>` 是生成密钥的年月，不能写成以后的月份（CI 会拒绝）。kid 永不复用：新 key 一定用新 kid。
- **每把 key 最多用 13 个月**：到 `<yyyy>-<mm>` 之后第 13 个月的 1 日（UTC）为止，例如 `mlu-host01-2026-11`
  在 2027-12-01 失效。sglang 也可能给某把 key 设更早的日期。到期前约 30 天 sglang 会通知你们，届时按下文轮换。

### 3. 提 PR

把公钥原样放到 `keys/<vendor>/<kid>.pub`。这个 PR 必须：
- 由 `vendors/<vendor>/submitters.txt` 里的账号发起；
- 只改 `keys/` 下的文件；
- 不修改已有的密钥文件。

CI 会检查以上几点，以及 kid 的格式和年月、公钥的格式，并打印每个 `.pub` 文件的到期日和 SHA-256。
请和主机上 `sha256sum <kid>.pub` 的结果比对。之后由 sglang 的 code owner 批准并合入。

**绝不要提交 `.key` 文件。** 一旦提交，即使之后删除，私钥也已经泄露：关掉 PR，
通知 sglang，用新 kid 重新生成一对密钥。

### 4. 等 sglang 通知生效，再启动循环

合入后，sglang 把公钥同步进 broker 并部署。生效后 sglang 会通知你们，并提供 broker 的基础地址
（`BROKER_URL`，例如 `https://<broker>`，不带路径）和你们可用的 labels。在此之前发请求会返回 `401 unknown kid`。

## 运行循环

`client/runner_loop.sh` 负责一个 runner 槽位：领配置 → 跑一个 job → 清理 → 重复。
每个槽位跑一个实例，例如用 `examples/systemd/` 里的 systemd 模板。主机必须开启 NTP：
token 有效期 60 秒，broker 只容忍 30 秒时钟偏差。

### 两种模式

| | 模式 A：`RUNNER_MODE=container`（默认） | 模式 B：`RUNNER_MODE=host` |
|---|---|---|
| job 在哪里跑 | 在本机，每轮一个全新的 `docker run --rm` 容器 | 在别处：runner 只是瘦客户端，把 job 交给你们自己的调度器（如 Jenkins） |
| 本机是否运行 PR 代码 | 是，在容器里 | 否；workflow 不在本机 checkout PR 代码 |
| 隔离方式 | runner 进程本身跑在容器里，私钥留在宿主机。如果 `DOCKER_ARGS` 挂载了私钥（或其上层目录）、用了 `--privileged`、或挂载了 docker socket，脚本拒绝启动 | 每轮把一份干净的 runner 安装目录复制到新的临时目录，以 `RUNNER_USER` 身份运行，结束后删除。`RUNNER_USER` 必须不同于运行循环的用户，并且读不到私钥；脚本会检查这两点 |

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `BROKER_URL` | 是 | broker 的基础地址，由 sglang 提供，例如 `https://<broker>`；客户端会自己加上 `/v1/jit/<VENDOR>` |
| `VENDOR` | 是 | 厂商名，如 `mlu` |
| `KID` | 是 | 本机 key 的 kid |
| `KEY_PATH` | 是 | 本机私钥（`<kid>.key`） |
| `RUNNER_LABEL` | 是 | 如 `mlu-pr`；每个实例只申请这一个 label |
| `RUNNER_HOST` | 否 | 默认 `hostname -s`；必须等于 kid 里的 `<host>` |
| `RUNNER_MODE` | 否 | `container`（默认）或 `host` |
| `CLIENT` | 否 | `jit_client.py` 的路径，默认和脚本同目录 |
| `RUNNER_IMAGE` | 模式 A | 装有 actions/runner、工作目录下有 `./run.sh` 的镜像 |
| `DOCKER_ARGS` | 模式 A，可选 | 追加给 `docker run` 的参数：设备、只读缓存挂载 |
| `RUNNER_DIR` | 模式 B | 一份干净的 actions/runner 安装目录，`RUNNER_USER` 要能读 |
| `RUNNER_USER` | 模式 B | 运行 runner 的用户；运行循环的用户需要免密 `sudo -u $RUNNER_USER` |
| `MIN_JOB_SECONDS` | 否 | 默认 30；runner 早于这个时间退出算一次失败 |
| `BACKOFF_BASE_SECONDS`、`BACKOFF_MAX_SECONDS` | 否 | 默认 5 和 600 |

同一台主机上的多个槽位可以共用这台主机的 key 和 `RUNNER_HOST`，broker 会给每个 runner 名加随机后缀。

```bash
sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now sglang-jit-runner@pr1
```

## 查看排队

要按需求决定开多少个 runner，可以问 broker 现在有多少你们的 job 在等 runner。任何一台主机的 key 都可以：

```bash
client/jit_client.py queue --url "$BROKER_URL" --vendor mlu \
  --kid mlu-host01-2026-11 --key /etc/sglang-ci/mlu-host01-2026-11.key
```

输出 broker 返回的 JSON：`queued`（总数）、`by_label`（每个 label 在等的 job 数）、
`oldest_queued_at`（UTC，没有时为 `null`）、`key_labels`（这把 key 能注册的 label，
也就是这台主机该开 runner 的 label）和 `jobs`（最早的 100 个，带仓库、workflow、job 名和 run 链接）。

- 这些数据来自 GitHub 的 job 事件，只能作参考，不是精确计数：丢了事件时，某个 job 可能不出现，
  或者已经结束的 job 一直留在列表里，直到排队满 24 小时。
- 每把 key 每分钟最多查 30 次，和领 runner 的配额分开算，轮询不会拖慢循环。

## 轮换或移除密钥

- **轮换**（最晚在 sglang 通知某把 key 30 天内到期时）：用新 kid 生成新密钥对并提 PR，等通知生效后，
  在主机上切换 `KID`/`KEY_PATH` 并重启，再提一个删除旧 `.pub` 的 PR。
- **下线主机**：提 PR 删除它的 `.pub`。删除 PR 任何人都可以发起。
- **私钥泄露或主机被入侵**：立即通知 sglang。sglang 会在 broker 上直接吊销这个 kid，
  不需要等 PR，几分钟内生效。被吊销的 kid 永远不能再用。

## 排障

| 返回 | 通常的原因 |
|---|---|
| `401 unknown kid` | key 还没生效、已被移除，或 `KID` 拼错 |
| `401 invalid token: ...` | 私钥和登记的 `.pub` 不匹配，或 `VENDOR` 填错 |
| `401 token issued in the future` | 主机时钟超前，检查 NTP |
| `401 key <kid> is revoked` | kid 已被吊销；用新 kid 生成新密钥 |
| `401 key <kid> expired on ...` | key 已超过 13 个月（或 sglang 设定的日期）；轮换 |
| `403 key <kid> does not belong to vendor ...` | `VENDOR` 和 kid 里的厂商不一致 |
| `403 source ip not allowed` | 出口 IP 不在该厂商的白名单里 |
| `403 labels not allowed for key ...` | `RUNNER_LABEL` 不在这把 key 的 labels 里 |
| `403 key <kid> may only register host ...` | `RUNNER_HOST` 和 kid 里的 `<host>` 不一致 |
| `404` | `BROKER_URL` 里带了路径，或客户端是旧版；改用基础地址和本 repo 的客户端 |
| `429 rate limit exceeded ...` | 超过这把 key 或该厂商的限流；循环会自动退避 |
| `429 queue read limit exceeded for key <kid>` | 这把 key 每分钟查排队超过 30 次，降低轮询频率 |
| `429 host <host> already has N runners` | 这台主机的 runner 数已到上限；runner 跑完会释放，从没上线的 runner 大约 15 分钟内会被清理 |
| `5xx` | broker 或 GitHub 侧的问题；循环会重试，持续出现请通知 sglang |
