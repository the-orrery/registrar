# registrar

`registrar` 是本机 workspace 资产登记和生命周期控制面。它回答三件事：

1. workspace 里有哪些资产；
2. 每个资产的生命周期 owner 是谁；
3. 迁移、关闭或清理前有哪些 finalizer 必须满足。

本仓只放工具代码。真实 registry 数据应放在仓库外；需要沙盒预览时可用
`REGISTRAR_REGISTRY_ROOT` 或 `--registry-root` 覆盖。

## 常用命令

```sh
registrar inventory --format table
registrar seed --format yaml
registrar doctor --format table
registrar relocate --dry-run <asset-name-or-path>
registrar relocate --dry-run --broad-sweep <asset-name-or-path>
registrar relocate --apply <asset-name-or-path>
registrar closeout --dry-run <asset-name-or-path>
registrar worktree create <repo-path> --owner-ref <ISSUE>
registrar worktree register <worktree-path> --owner-ref <ISSUE>
registrar worktree audit
registrar worktree remove <worktree-name-or-path> --apply
```

`relocate --apply` 会执行目录迁移：重写 live functional refs
（symlink、launchd plist、shell/config 文件），更新 registry record，最后移动目录。
任一步失败都会回滚。它不会自动 commit，不会自动 reload launchd，也不会重写需要人工复核
的 preserved 内容（PM data、archive、runtime record、`.md` 文档）。

高风险迁移先跑：

```sh
registrar relocate --dry-run --broad-sweep <asset>
```

`workspace_sweep_refs` 是全 workspace 扫出来的 review-only 引用，不会被
`--apply` 自动消费。

沙盒预览建议把 registry override 放在 subcommand 前：

```sh
registrar --registry-root /tmp/example-registry relocate --dry-run --broad-sweep example-asset
```

顶层 `closeout --apply` 仍只保留 legacy dry-run 规划；worktree 生命周期使用
`registrar worktree audit` / `registrar worktree closeout`。

## Worktree 登记

agent 在 `$REGISTRAR_WORKSPACE_ROOT/worktrees` 下新建工作树时，不要直接用 raw
`git worktree add`。推荐入口是：

```sh
registrar worktree create <repo-path> --owner-ref TASK-542
```

它会同时完成两件事：

1. 创建 git worktree；
2. 写入 registrar `Worktree` 归属记录。

已经手工创建的 worktree 用：

```sh
registrar worktree register <worktree-path> --owner-ref TASK-542
```

这两个命令默认执行；需要预览时加 `--dry-run`。`--owner-ref` 必须是 issue id
（例如 `TASK-542`）。如果没有 issue，先用 `docket new` 创建或复用一个 PM issue。
`none:<reason>` 只用于明确临时例外，必须额外传 `--allow-unowned`。`world` 会优先从
source repo registry/path 或 issue prefix 推导；推不出来时再传 `--world personal|work`。

当 registry root 位于 git work tree 时，`create` / `register` / `closeout` 会自动把
**单个**记录文件 commit 进 registry 仓（pathspec 限定，不会把无关脏改扫进提交）；
registry 不在 git 仓内时此步为 no-op。

工作树收口先看 issue owner，再看本地安全条件：

```sh
registrar worktree audit --owner-ref TASK-542
registrar worktree closeout <worktree-name-or-path> --dry-run
registrar worktree closeout <worktree-name-or-path> --apply
registrar worktree remove   <worktree-name-or-path> --apply   # closeout 的别名
```

`remove` 是 `closeout` 的别名，便于发现；它**不是** raw `git worktree remove`，
而是同一套受闸的生命周期收口（查 issue owner、判分支是否合并、写 finalizers）。

默认 closeout/remove 会删除 worktree 和 active registrar record，但保留 git branch。
默认安全闸会在以下情况阻断，需显式逃生口：

- 工作树有改动/未跟踪文件 → `--force`（改走 `git worktree remove --force` 丢弃）；
- issue owner 未关单 → `--owner-active-ok`；
- 分支未并入 default → `--allow-unmerged`；
- active record 指向的路径已不存在 → `--stale-record`。

需要顺手删本地分支（默认保留、永不删 default 分支）时加 `--delete-branch`。

## Registry Record

registry 文件是 registry root 下的 YAML 或 JSON 文档：

```yaml
apiVersion: registrar.local/v1alpha1
kind: Repo
metadata:
  name: control-plane
  path: $REGISTRAR_WORKSPACE_ROOT/control-plane
  labels:
    repo: control-plane
spec:
  owner_ref: TASK-542
  lifecycle: active
  placement: workspace/root
  restore_policy: source-of-truth
  allowed_actions: [inspect, relocate-dry-run, closeout-dry-run]
finalizers:
  - pm-owner-required
  - closeout-recorded
```

`spec` 是期望状态；`status` 由 `inventory` 和 `doctor` 生成。已明确移除的资产用
`kind: Tombstone`，不写 `metadata.path`，历史位置放在
`metadata.labels.old_path`。

## 开发验证

```sh
uv sync
uv run registrar --help
uv run poe check
uv tool install --force --reinstall --refresh .
```
