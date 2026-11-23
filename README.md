# 澄桥试验中心 · 夜班称量核对

两个互相独立的 SQLite 库完成“加工”与“封账”：

| 库 | 文件 | 职责 |
| --- | --- | --- |
| 加工库 | `--process process.db` | 冻结具名规则、接单、按路径推进就绪项、产生终点件、生成加工端回执（提交件） |
| 账库 | `--account account.db` | 申报提交件、逐件入账、核对全部预期路径、封账、复领 |

账端只接收加工端交付的**原件文本**（提交件与终点件各自带摘要），从不打开加工库，也不读它的表。
所有动作一次一个 SQLite 事务。

## 规则

规则是一张有向无环图。每个工序节点二选一：

- **终点节点** `{"category": "类别"}`；
- **分支节点** `{"limit": 非负整数, "left": "节点名", "right": "节点名"}`。

分支分配是确定的：左取 `min(来量, 上限)`，右取余量。冻结时拒绝环路（含自环）和指向规则外节点的坏引用；
同一规则号重复冻结必须内容一致，否则拒绝。

## 推进

每个“就绪项”是某次称量到达某节点的一次出现，身份为
`(接纳号, 原号, 全路径)`；全路径是从起点走过的具名边序列（`L`/`R`）。

- 分支节点推进时一次分为左右两路；两路都是新的就绪项或终点件；
- 终点节点推进时记录一件终点件（含接纳号、规则号、原号、路径、类别、克数）；
- 汇流不合并：两条不同路径到达同一节点是两个独立出现，各自保留完整路径与克数；
- 零克照常走完全部路径；
- 重复推进已提交项返回**原结果**（`replayed: true`），不改账、不重复记件。

样例 `R` 左右入 `A`、`B`，`A`、`B` 各左入 `Z`、右入 `M`，`M` 左入 `D`、右入 `E`，
`R`、`A`、`B`、`M` 上限依次 5、0、0、2。`R` 入 12 克得到：

```
LL  Z  0
LRL D  2
LRR E  3
RL  Z  0
RRL D  2
RRR E  5
```

## 接单

接单给批号、规则号与有序称量（每项唯一原号、起点、非负整数克数）。
同批同参复领返回**同一接纳号**；同批异参拒绝（`param-conflict`）。

## 账端

- `declare`：只收加工端已提交的提交件。逐件重算摘要与清单摘要，文本不符即拒绝。
- `post-piece`：只收已申报提交件下的终点原件。错绑（`submission_id` 不符）拒绝；
  与预期路径/内容不同的文本拒绝；未申报先到的件拒绝。
- 入账按出现 `(接纳号, 原号, 路径)` 只记一次；允许乱序、允许失答重投（返回 `replayed: true`）。
- `seal`：只凭账库材料核对**全部预期路径**的实收，缺件返回 `pending` 并列出缺项，不产生任何成果；
  同一接纳号重试只签一次（`replayed: true`，成果完全相同）。

## 成果

```
result_id / receipt_id / submission_id / rule_id / batch
receipt         原回执（提交件）
detail          按 原称次 再按边序（L 先于 R）排列：
                orig_no, path, category, grams, piece_id, content_digest
totals          按类别合计（类别名排序）
total_grams     全部克数
```

## 命令行

```bash
python3 -B -m chengqiao --process P.db freeze-rule rule.json
python3 -B -m chengqiao --process P.db accept-order order.json
python3 -B -m chengqiao --process P.db ready   B1-0001
python3 -B -m chengqiao --process P.db advance B1-0001 W1 ""      # 起点
python3 -B -m chengqiao --process P.db advance B1-0001 W1 "L"
python3 -B -m chengqiao --process P.db pieces  B1-0001
python3 -B -m chengqiao --process P.db submit  B1-0001 > submission.json

python3 -B -m chengqiao --account A.db declare   submission.json
python3 -B -m chengqiao --account A.db post-piece piece.json
python3 -B -m chengqiao --account A.db seal      B1-0001
python3 -B -m chengqiao --account A.db lookup    B1-0001        # 或 SEAL-B1-0001
```

`-`（默认）表示从标准输入读 JSON。被拒绝的动作在 stderr 输出
`{"error": 代码, "message": 说明, "details": ...}` 并以状态 2 退出。

## 运行清单入口

- `setup`：仅检查模块可导入（纯标准库，无安装步骤）。
- `verify`：`python3 -B -m chengqiao.verify`——跑完整测试，再跑 CLI 场景：
  失答重投（推进/提交/入账/封账）均得原结果，缺件封账与错绑件等失败路径退出非零。
- `demo`：`python3 -B -m chengqiao.demo`——样例全流程、乱序入账、失答重投、
  关闭加工库后只重开账库按接纳号与成果号复领，并区分未知与待完成。

## 复领语义

- `sealed`：已封账，返回完整成果；
- `pending`：已申报但未齐/未封，列出 `missing`；
- `unknown`：账库没有该接纳号（或该成果号）的材料。
