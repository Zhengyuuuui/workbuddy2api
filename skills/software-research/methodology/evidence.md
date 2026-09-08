# Evidence & Hypothesis Management

本文件定义证据分级、证据链、假设管理与结论修正的规则。
是所有研究阶段共享的基础层。

## 证据分级

| 等级 | 定义 | 判断标准 |
| -- | -- | -- |
| **Confirmed** | 直接、可重复的证据 | 实际运行结果 / 可复现行为 / 明确源码逻辑 / 明确调用结果 / 明确网络响应 |
| **Strong Evidence** | 多个独立证据共同支持，尚未完全直接验证 | 至少 2 个独立来源一致指向同一结论 |
| **Hypothesis** | 合理推测，尚未验证 | 由观察推导出的候选解释 |
| **Unknown** | 信息不足，无法确定 | 缺乏任何可支撑线索 |

**硬规则**：
- 禁止将 Hypothesis 写成 Confirmed。
- 禁止将 Strong Evidence 写成 Confirmed（它们之间隔着一次直接验证）。
- Unknown 时明确说"不知道"，不要用猜测填充。

## Static vs Runtime 证据

```
Static Artifact（配置/字符串/二进制/源码/清单/元数据）
  描述：声明（意图）
Runtime Behavior（内存/进程/网络/日志/缓存/IPC/文件变更/系统调用）
  描述：事实（发生的事）
```

- 静态证据可以产生 Hypothesis 与 Strong Evidence。
- 只有运行时证据能把结论升级为 Confirmed。
- 静态与运行时冲突时：**记录冲突**，不得默认某一方正确，设计实验裁定。

### 关键认识

> 静态值（如配置声明的服务地址）不等于运行时实际使用的值。
> 运行时配置、缓存、环境变量、重定向都可能改写静态声明。
> 在把静态值当作事实之前，必须用运行时证据确认。

## 证据链

每条关键结论必须可追溯：

```text
Claim                    ← 结论
  └── Evidence           ← 支撑它的证据（原始观察/输出/响应）
        └── Source       ← 证据来自哪（哪个文件哪一行 / 哪条命令 / 哪个进程 / 哪个请求）
              └── Verification Method  ← 如何得到/复现
                    └── Confidence     ← 基于证据等级与独立来源数
```

规则：
- 证据优先引用**原始输出**（命令 stdout、响应体、配置文件原文），而非转述。
- 每个结论标注置信度（Confirmed / Strong / Hypothesis / Unknown）。
- 关键结论尽量有 ≥2 个独立来源（跨文件、跨方法、跨层）支撑。

## 假设管理

每个活跃假设应登记：

```text
H1: <假设>
  Evidence for: <支持它的证据>
  Evidence against: <反对它的证据>
  Confidence: <假设/Strong/Confirmed>
  Status: <active / refuted / confirmed>
```

- 多个并存假设不要任选一个推进，按信息增益排序。
- 被证伪的假设**保留记录**并标注 refuted（避免重复推导）。
- 假设一旦被确认，升级为结论并进入证据链。

## 模型更新与结论修正

新证据进入时按此流程：

```text
Old Model
  ↓
New Evidence
  ↓
冲突？ → 是：降低置信度，明确记录 "Previous hypothesis was incorrect"
  ↓
支持？ → 是：可升级置信度
  ↓
更新模型，进入下一轮
```

规则：
- 新证据推翻旧结论时，**显式记录推翻**，不悄悄改口。
- 每次模型更新后重新评估剩余不确定性，决定是否继续。
- 目标已经解释清楚即停止扩大范围（Stop Expanding Scope）。
