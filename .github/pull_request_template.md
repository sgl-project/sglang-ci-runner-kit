<!--
Adding host keys? Fill in the table below. Otherwise delete this template.
提交主机公钥时请填写下表；其他 PR 请删除本模板。
-->

## Hosts / 主机

One row per new kid. sglang uses this to set the limits for each key in the broker.
每个新 kid 一行。sglang 据此在 broker 里设置这把 key 的上限。

| kid | Cards / 卡数 | Labels it will run / 会跑的 labels | Runner slots / 同时运行的 runner 数 | Replaces / 替换的 kid |
|---|---|---|---|---|
| `<vendor>-<host>-<yyyy>-<mm>` | 8 | `<vendor>-1card`, `<vendor>-8card` | 8 | |

- Slots: the most runners this host will have registered at once, across all its labels.
  同时运行的 runner 数：这台主机所有 label 加起来，最多同时注册多少个 runner。
- Replaces: for a rotation, the old kid. Leave empty for a new host.
  替换的 kid：轮换时填旧 kid；新主机留空。

## Checklist / 检查

- [ ] Only `keys/<vendor>/<kid>.pub` files; no `.key` file. / 只有 `.pub` 文件，没有 `.key`。
- [ ] The SHA-256 in the CI summary matches `sha256sum <kid>.pub` on each host. / CI 打印的 SHA-256 与主机上的一致。
