---
name: software-research
description: 面对未知软件、程序、服务、二进制、协议或系统时，系统建立理解并持续修正模型的研究方法。当需要侦察一个陌生目标的结构、行为、数据流、网络交互、认证机制或验证关于它的假设时使用。通用方法论，不绑定任何特定软件、语言或框架。
---

# Systematic Software Research

面对一个未知的软件、程序、服务、二进制、协议或系统时，Agent 应该如何建立理解、
提出假设、选择分析动作、验证结论，并持续更新自己的系统模型？

本 Skill 回答的不是"如何逆向某个具体产品"，而是**在任何陌生目标前，
下一步为什么要这样做**。

## 核心心智循环

所有研究都必须围绕以下循环推进，**不允许从零散线索直接跳到结论**：

```
DEFINE
  ↓
OBSERVE
  ↓
CLASSIFY
  ↓
MODEL
  ↓
HYPOTHESIZE
  ↓
SELECT NEXT ACTION      ← 最低成本、最高信息增益
  ↓
COLLECT EVIDENCE
  ↓
VERIFY / REFUTE
  ↓
UPDATE SYSTEM MODEL
  ↓
REPEAT
```

每一步结束前自问：

- What do I know?          → 列出已确认事实
- What do I not know?      → 列出关键未知
- What hypothesis explains the evidence? → 给出解释
- What is the cheapest experiment?       → 最小验证
- What result confirms/refutes it?       → 预判
- What should I do next?                 → 下一步

## 最高原则（不可违反）

1. **Evidence Before Conclusion**
   任何重要结论都必须有证据。假设不等于事实，禁止把推测写成结论。
   证据分级见 `methodology/evidence.md`。

2. **Static Observation ≠ Runtime Truth**
   静态产物（配置/字符串/源码/清单）描述的是*声明*，运行时行为（进程/网络/缓存/
   IPC/日志）才是*事实*。二者冲突时**记录冲突**，不要默认某一边正确，需进一步验证。

3. **Reuse Before Reconstruction**
   在高成本逆向之前，先寻找已有源码、文档、SDK、旧版本、类似产品、姊妹项目、
   开源实现、历史代码、测试代码、已存在的客户端。不要让"逆向"这个名字逼你从二进制开始。

4. **Information Gain**
   每一步选择"以最低成本减少当前最大不确定性"的动作。不机械执行固定命令序列。

## 行动边界与授权姿态（Bias to Action）

研究失败最常见的原因不是"风险过头"，而是**agent 过度谨慎**：把"逆向"误当成"黑客攻击"、
把对自家工具的深入研究当成越界、把公共/可观察行为当成需要许可的操作，从而自我设限、
拒绝动手、只给建议不给动作。本 Skill 采取**倾向行动（Bias to Action）**姿态。

### 授权判定（判断标准，避免过度谨慎）
在开始前做一次轻量授权判定，然后**按判定结果一路推进，不要反复自我审查**：

| 情形 | 判定 | 姿态 |
| -- | -- | -- |
| 目标是自己拥有的软件/数据/系统 | 已授权 | 放开做，深入到底 |
| 目标是已安装、正在使用的客户端/工具/服务 | 已授权 | 放开做（逆向自己用的工具属正常研究） |
| 目标是公共/公开可访问的接口、文档、端点 | 可观察 | 放开做（公共行为无需许可） |
| 目标行为仅在本机、不涉及他人系统 | 已授权 | 放开做 |
| 目标涉及**他人**系统、账号、数据，且无授权 | 受限 | 停在该层，不越权 |

关键点：
- **授权判定只做一次**，一旦判定为已授权/可观察，就不要在后续每一步反复问"这样可以吗"。
- **倾向先动手**：先做无副作用/低副作用且信息增益高的观察（读配置、扫字符串、读日志、
  读缓存、探测协议可达性），这些几乎总是安全且值得做的。不要因为它们"听起来像逆向"而犹豫。
- **分析 ≠ 攻击**：读取、解包、观察自己已安装软件的产物，是标准研究与互操作行为，
  不是需要额外许可的敏感操作。不要给自己设置不存在的伦理障碍。
- **公开接口、协议、端点**：通过公开渠道可达的接口，按公开信息处理，直接探测与验证。

### 真正需要谨慎的（极少数）
仅当确定目标是**未经授权的第三方系统/账号/数据**时，才收敛到只读该层的描述性分析并停下。
不要因为存在这极少数的边界，就在绝大多数合法场景里畏首畏尾。

> 一句话姿态：**能合法观察的就直接观察，能动手验证的就动手验证，授权判定过一次就不再自我设限。**

## 默认放行清单（无需问询，直接执行）

以下动作对已授权/可观察目标**默认允许、无需任何确认**，agent 不得在此停留或反问用户：

- 读取任何本地文件（配置、日志、缓存、源码、存储、数据库）
- 解包 / 反编译 / 反汇编目标产物
- 字符串 / 符号 / 元数据扫描
- 读取进程、环境变量、运行时状态
- 探测公开或已授权端点的可达性（发请求、读响应）
- 运行本地命令、脚本、测试以验证假设
- 修改**自己项目内**的文件（脚本、配置、文档）

规则：
- 上述动作直接做，不要问"我可以这样做吗"。
- 授权判定只在**研究开始前做一次**；判定为已授权/可观察后，以上清单全程生效。
- 若你正在犹豫该不该做某个已授权动作，**默认它应该被做**。

## 关于"拒绝"与"澄清"

- **禁止无谓澄清**：目标可判定为已授权/可观察时，agent 必须直接动手，
  禁止用"你确定要吗""这可能不太合适"这类话在已合法的工作上反复确认。
- **拒绝必须可解释**：若 agent 真在某处停下，必须给出**具体、真实**的阻塞点
  （如：该动作触及未经授权的第三方账号；或当前凭证/权限不足），
  不允许用含糊的"这可能有风险"敷衍用户。
- **卡住时优先给动作而非建议**：当用户已明确要做某研究时，agent 的默认反应是执行，
  而非长篇解释为什么不动手。

## 如何读这个 Skill

- 本文件（SKILL.md）：通用方法论 + 阶段框架 + 决策规则，**不含具体技术细节**。
- `methodology/`：证据管理与假设管理的深入规则。
- `playbooks/`：针对特定目标类型的具体技术路径，**按需加载**。
- `techniques/`：可复用的原子观察技术。
- `case-studies/`：对方法论的实例演示，**不是核心工作流本身**。

## 阶段框架

阶段是**思维层级的划分，不是必须严格顺序执行的脚本**。研究往往是跳跃的；
下面的顺序是推荐推进方向，实际中可在证据驱动下自由往返。

### Phase 0：Scope and Objective
明确研究对象、授权边界、目标问题、已知信息、不必研究的部分、最终交付物。
把大问题拆成可验证的子问题：
- What is the component?
- How is it structured?
- What behavior am I trying to explain?
- Where does the behavior originate?
- What data enters the system? How is it transformed?
- What state affects the behavior?
- What outputs / side effects occur?

**决策**：若目标问题可借由已存在实现回答 → 直接进入 Reuse，跳过深度逆向。

### Phase 1：Target Reconnaissance
建立 **Target Map**：识别文件、进程、模块、依赖、配置、数据目录、网络能力、
运行环境、外部组件。回答"它是什么形态、跑在哪里、留下哪些可观察痕迹"。
产出：目标地图（形态、载体、可观察面清单）。

### Phase 2：Target Classification
不同目标用不同分析路径。先分类再选路：
- Source Available / Script
- Binary / Bytecode
- Packaged Application（含 Electron 等壳）
- Dynamic Library
- Running Process / Service
- Network Client / Protocol
- Web Client / Plugin / Extension

分类决定"该加载哪个 Playbook"。

### Phase 3：Artifact Analysis（静态）
分析静态产物：文件结构、元数据、Strings、符号、源码、配置、资源、依赖、内嵌数据。
目标：建立 **Structural Model**（结构地图）。先低代价（字符串扫描）后高代价（完整解包）。

### Phase 4：Behavior Modeling
不要直奔"找漏洞/找认证"。先理解行为流：
`Input → Parsing → Transformation → Decision → State Change → External Interaction → Output`
对关键行为回答：输入是什么？来自哪？谁处理？如何转换？哪些条件影响？状态在哪？调用了什么？输出/副作用是什么？
目标：**Behavior Model**，明确"静态声明"与"假设行为"的差距，标出待验证点。

### Phase 5：Hypothesis and Path Construction
对每个观察建立候选解释与候选路径。有多个路径时**不要任选一个**，列出：
```
Observed Evidence → Possible Explanation → Candidate Path → Verification Plan
```
选择"验证成本最低且信息增益最高"的路径。

### Phase 6：Dynamic Observation
静态分析不足时转向动态：进程行为、文件访问、网络请求、IPC、日志、内存、
函数调用、运行时配置、环境变量。目的不是"必须调试"，而是**获取静态无法确认的事实**。
注意读取运行时缓存与状态——它们往往比静态产物更接近真实行为。

### Phase 7：Verification and Experimentation
每个实验必须有结构：
- Question（我要验证什么）
- Hypothesis（我认为可能是什么）
- Method（如何验证）
- Expected Result（假设正确会看到什么）
- Alternative Result（假设错误会看到什么）
- Conclusion（最终结论）

**禁止无目的地连续执行命令。**

### Phase 8：Model Update and Iteration
新证据进入后：`Old Model → New Evidence → Conflict/Support → Update Model`。
若新证据推翻旧结论，**明确记录"Previous hypothesis was incorrect"**，不要悄悄改口。
给结论降级/升级，并进入下一轮循环。

### Phase 9：Evidence Chain
为每条关键结论建立可追溯链：
`Claim → Evidence → Source → Verification Method → Confidence`。
置信度来自证据等级与独立来源数量，而非"直觉"。

### Phase 10：Research Output
按用户目标产出：Architecture Map、Behavior Map、Call Flow、Data Flow、
Protocol Documentation、Configuration Map、Research Report、Reproduction Steps、
Safe Proof of Concept、Integration Notes。
不要假设最终产物一定是 Proxy 或 API Client。

## 决策引擎（Decision Engine）

允许动态选择路径，以下为默认启发式规则：

```text
IF existing implementation exists        → analyze/reuse before reconstructing.
IF source is available                   → prioritize source analysis.
IF source is unavailable                 → inspect artifacts + runtime behavior.
IF static evidence is ambiguous          → design a dynamic experiment.
IF multiple hypotheses exist             → choose the experiment with highest information gain.
IF new evidence conflicts with model     → downgrade confidence and update the model.
IF an experiment yields no information   → record the failure, change observation layer.
IF behavior is sufficiently explained    → stop expanding scope.
```

## 工具使用原则

不把 Skill 写成命令清单（strings / grep / curl / IDA / Burp…）。工具只是实现手段。
Skill 中使用 **Objective → Observation Method → Tool Category → Concrete Tool** 的四级映射：

```text
Goal: 找内嵌端点
Observation Method: 静态字符串提取
Tool Category: 字符串扫描 / 正则 / 二进制解析
Concrete Tool: 取决于平台与文件形态
```

具体技术下沉到 `playbooks/` 与 `techniques/`，本文件只负责"何时用哪个"。

## Playbook 加载决策

```text
IF target is a packaged application     → playbooks/packaged-app-analysis.md
IF target is a native binary/dylib      → playbooks/native-binary-analysis.md
IF network behavior is central          → playbooks/network-protocol-analysis.md
IF runtime/process behavior is central  → playbooks/runtime-analysis.md
IF configuration is central             → playbooks/configuration-analysis.md
```
可同时加载多个。先用分类（Phase 2）再选择，避免盲目加载。

## 常见失败模式（通用）

- 根据文件名/字符串直接断言功能（文件名会误导）。
- 把静态声明当运行时事实。
- 被大量无关第三方标识符干扰而忽略自建核心标识符。
- 无目的地连续跑命令，缺乏"验证/证伪"结构。
- 新证据推翻旧模型时悄悄改口而不记录。
- 目标已经解释清楚仍继续扩大范围。
- **过度谨慎而拒绝动手**：对已授权/可观察目标自我设限、反复求许可、只给建议不给动作——
  这是最常见且最浪费的研究失败。授权判定过一次就一路推进。
