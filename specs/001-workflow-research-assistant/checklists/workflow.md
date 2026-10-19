# 工作流需求质量 Checklist：工作流驱动的信息研究助理（一期）

**Purpose**：审查一期聊天确认与固定流程交付，以及恢复、分支、额度和串行约束的完整性、清晰度、一致性与可衡量性。
**Created**：2026-10-19
**Feature**：[spec.md](../spec.md)
**Context**：[plan.md](../plan.md)、[tasks.md](../tasks.md)、[项目宪章](../../../.specify/memory/constitution.md)；任务清单已生成（T001–T103），本清单条目仍待评审者裁定。
**Depth / Audience / Timing**：Standard；需求及 PR 评审者；实施前与相关变更评审。
**Scope**：一期为主；二期仅审查分期边界，不作为一期发布前置。用户本次未附加必查项，采用当前交付目标推导范围。

**Note**：本自定义清单由 `$speckit-checklist` 根据当前功能文档生成，用于审查需求文字质量。
**Review Ownership**：本清单由评审者维护；仅当评审者判断相应需求质量准则已满足时才标记 `[x]`。
**Marker Semantics**：`[x]` 表示需求质量审查通过，不表示实现工作完成。本次已裁定 9 项（CHK006—009、015、016、018、036、038），其余 29 项仍待评审者裁定。

## Requirement Completeness — 需求完整性

- [ ] CHK001 一期选流、聊天补齐、完整要求确认、固定流程研究和文档交付的必需输入、输出及阻塞条件是否均有明确要求？ [Completeness, Spec §FR-001, Spec §FR-002, Spec §FR-004, Spec §FR-028, Spec §FR-031]
- [ ] CHK002 对明确输入、默认值、待确认推断及建议的来源区分，以及“接受建议”与“确认完整要求后执行”的不同授权含义，是否均有完整定义？ [Completeness, Spec §FR-002, Spec §FR-004, Spec §SC-013]
- [ ] CHK003 根据本次方向引导喜好、允许无特别偏好、冲突取舍和最终优先顺序确认的要求是否覆盖全部输入情形？ [Completeness, Spec §FR-001, Spec §FR-010, Spec §用户故事1]
- [ ] CHK004 每个查询方向的结果或缺口、事实来源与时间、事实/评价/推断区分，以及硬约束未知项的处理要求是否齐全？ [Completeness, Spec §FR-008, Spec §FR-009, Spec §FR-010]
- [ ] CHK005 文档结构、语言、详略、交付形式、必要栏目和不支持格式的事前协商要求是否完整，并有明确的部分完成与交付失败定义？ [Completeness, Spec §FR-011, Spec §FR-012, Spec §用户故事4]

## Requirement Clarity — 需求清晰度

- [x] CHK006 “必要信息完整”“硬约束冲突”和“适用范围”是否具有足以判定允许确认或必须追问的业务依据，而非仅依赖助理主观判断？ [Clarity, Spec §FR-002, Spec §FR-031, Ambiguity]
- [x] CHK007 “重要事实”“过期内容”“关键条件未知”及“合格推荐”是否有可复核的定义或由具体工作流明确的判定依据？ [Clarity, Spec §FR-009, Spec §FR-010, Spec §SC-005, Ambiguity]
- [x] CHK008 “最新已确认可用版本”的确认主体、可用条件和草稿转为可用的时点是否明确，且适用于一期手动修改的工作流？ [Clarity, Spec §FR-020, Spec §FR-021, Spec §用户故事7, Ambiguity]
- [x] CHK009 “最后可信完成步骤”“有效前置成果”的完整性、时效、适用性及不可确定时的判定责任是否清楚？ [Clarity, Spec §FR-025, Spec §FR-032, Spec §用户故事9.3, Spec §用户故事9.7]

## Requirement Consistency — 需求一致性

- [ ] CHK010 规格、规划是否一致地区分一期需求引导与二期协同探索，并保留一期直接选流及本次喜好，避免将模型推荐或长期偏好作为一期依赖？ [Consistency, Spec §交付期次, Spec §功能依赖关系, Spec §假设与范围说明12—13, Plan §Summary]
- [ ] CHK011 聊天修改后旧确认失效、改选流程重新确认、执行中版本固定的要求是否一致，并明确版本变化发生于确认前后时的边界？ [Consistency, Spec §FR-004, Spec §FR-006, Spec §FR-021, Spec §FR-031, Plan §核心执行决策]
- [ ] CHK012 需求摘要的三级标题要求是否与最终文档的约定格式明确区分，且没有因层级要求引入编造内容或强制表单？ [Consistency, Spec §用户故事1.5, Spec §FR-011, Spec §假设与范围说明12]
- [ ] CHK013 暂停、中断、取消、额度耗尽和模型结果未明的继续条件是否在故事、功能需求、成功标准与规划中一致，且不将取消视为可直接恢复？ [Consistency, Spec §FR-024, Spec §FR-025, Spec §FR-033, Spec §SC-016, Plan §核心执行决策]
- [ ] CHK014 跨分支累计预算、独立研究预算及草稿澄清额度之间的关系是否与“不通过分支或换任务绕过求助”的业务约束一致？ [Consistency, Spec §共通约束及验收·有界执行, Spec §用户故事9.9, Plan §默认政策, Conflict]

## Acceptance Criteria Quality — 验收标准质量

- [x] CHK015 重要事实来源覆盖率、必需栏目覆盖率和缺口披露率的分母、标注依据及完整/部分完成判据是否足够明确，使 100% 目标可客观衡量？ [Measurability, Spec §SC-005, Spec §SC-006, Ambiguity]
- [x] CHK016 UI 独立使用标准是否定义目标用户、初始配置完成的边界及允许的帮助范围，使“无开发人员代操作”的判断可重复？ [Measurability, Spec §SC-009, Spec §用户故事10.1, Ambiguity]
- [ ] CHK017 稳定性标准是否明确正常案例与异常案例的适用条件、预期文档完整程度，以及来源不足或额度不足时不计作完整成功的口径？ [Measurability, Spec §SC-008, Spec §SC-012]
- [x] CHK018 同时运行分支数和在途模型请求数的计数范围、起止边界及未知结果归属是否有可观测定义，足以判定上限为 1？ [Measurability, Spec §FR-033, Spec §SC-017]

## Scenario Coverage — 场景覆盖

- [ ] CHK019 空库、无适用流程、输入超出范围、改选流程及版本变化等替代路径，是否均定义保留哪些已收集要求、哪些必须重新确认？ [Coverage, Spec §FR-030, Spec §FR-031, Spec §边界情况, Gap]
- [ ] CHK020 来源失败、必要步骤失败、无合格结果、部分完成和文档获取失败的要求是否明确允许的替代路径、必须阻塞的依赖及可保留成果？ [Coverage, Spec §用户故事3.3, Spec §用户故事4.3—4.4, Spec §SC-008]
- [ ] CHK021 写入已生效、未生效、部分生效、结果未知及目标后来变化的恢复要求，是否分别定义核验依据、允许的处置和不能安全恢复时的用户路径？ [Coverage, Spec §FR-025, Spec §用户故事9.5, Spec §SC-012]
- [ ] CHK022 从首节点、较早节点和已有分支再次分叉时，是否完整定义父子关系、独立标识、成果引用及历史保留要求？ [Coverage, Spec §FR-032, Spec §SC-014]
- [ ] CHK023 分支修改仅影响后续、影响更早步骤、用户拒绝前移和影响不明的情形，是否均规定要求确认、起点确认与失效成果的处置？ [Coverage, Spec §用户故事9.8, Spec §SC-015]
- [ ] CHK024 额度触顶、用户未回应、仅讨论问题、只批准额度但未同意继续，以及明确批准并解决阻塞的情形，是否均有清楚的授权要求？ [Coverage, Spec §FR-024, Spec §用户故事9.9—9.10, Spec §SC-016]

## Edge Case Coverage — 边界覆盖

- [ ] CHK025 重复启动、多个窗口或任务竞争启动、运行时聊天、模型超时和停止时请求在途的需求，是否明确等待反馈与重新授权边界？ [Coverage, Spec §FR-033, Spec §SC-017, Gap]
- [ ] CHK026 停止请求后迟到的结果和实际已生效的写入，是否同时具备保护终态及保留真实操作记录的要求，避免二者相互冲突？ [Consistency, Spec §用户故事9.2, Plan §核心执行决策]
- [ ] CHK027 确认摘要过期、保存冲突、保存失败及重新打开页面的情形，是否明确待确认要求和未保存修改的保留期限与恢复边界？ [Coverage, Spec §FR-004, Spec §FR-022, Spec §用户故事7.4, Gap]
- [ ] CHK028 分支依赖的资料被删除、到期、损坏或不再适用时，是否明确历史可见性、复用禁止条件以及可行重跑起点？ [Coverage, Spec §FR-027, Spec §FR-032, Spec §A4, Plan §默认政策]

## Non-Functional Requirements — 非功能需求

- [ ] CHK029 性能目标是否明确数据规模、统计口径、观测起止点及模型耗时的排除范围，并将停止反馈速度与实际停止完成区分？ [Measurability, Plan §Technical Context, Spec §用户故事9.2, Gap]
- [ ] CHK030 资料最小化、外部访问授权、不可信内容、敏感数据脱敏及可审查摘要的要求，是否覆盖文档、状态、错误、日志和备份等相关输出？ [Completeness, Spec §FR-026, Spec §A1—A3, Plan §安全、迁移与运行]
- [ ] CHK031 数据保留、删除与备份恢复的要求是否明确时间起点、各类数据适用期限、关联分支失效及用户自行保存副本的责任边界？ [Clarity, Spec §FR-027, Spec §A4, Plan §默认政策, Gap]
- [ ] CHK032 完整可用 UI 是否明确键盘操作、焦点、状态信息可读性和目标屏幕范围的要求，或明确记录这些可用性要求的范围取舍？ [Completeness, Spec §FR-028, Spec §SC-009, Gap]

## Dependencies & Assumptions — 依赖与假设

- [ ] CHK033 一期至少两份不同适用范围的种子流程，是否有明确输入、查询方向、输出及可执行条件，并与完整闭环和稳定性验收样例对应？ [Traceability, Spec §假设与范围说明3, Spec §SC-001, Spec §SC-012, Plan §Structure Decision]
- [ ] CHK034 外部来源可用性和真实运行时能力是否明确为待验证依赖，并记录不可满足时的受限交付或阻塞条件，避免以模拟结果替代真实能力？ [Dependencies, Spec §假设与范围说明7, Plan §Constitution Check, Assumption]
- [ ] CHK035 状态和产物版本兼容、迁移失败及备份恢复的需求是否定义可信数据恢复条件、停止自动续跑和人工处置边界，并符合宪章的可恢复性原则？ [Completeness, Spec §FR-025, Plan §安全、迁移与运行, Gap]

## Ambiguities & Conflicts — 歧义与冲突

- [x] CHK036 “任意节点可选起点”与“更早节点创建分支”的表述，是否明确当前节点、尚未执行节点及不在既有路径上的节点应属于继续、重试、新分支还是不可启动？ [Clarity, Spec §用户故事9.4, Spec §用户故事9.6—9.7, Spec §FR-032, Ambiguity]
- [ ] CHK037 一期写入恢复要求是否明确覆盖范围内的文档保存等操作，同时与“不代用户对外发送或修改”的范围约束及规划工具限制一致？ [Consistency, Spec §共通约束及验收·行动边界, Spec §共通约束及验收·中断恢复边界, Plan §Architecture and Boundaries]
- [x] CHK038 模型远端结束长期无法确认时，是否定义用户可理解的处置路径及可用性限制，并明确该路径不能绕过单请求在途和未知结果核验要求？ [Completeness, Spec §用户故事9.12, Spec §FR-033, Plan §默认政策, Gap]

## 裁定记录（2026-10-19）

按 H2 方案 A 只裁定影响架构与验收口径的 9 项，其余 29 项保留为评审提示，不阻断一期实施。

- CHK006 → 通过。必要输入由流程定义的 required 输入与确定性校验判定，适用范围由 WorkflowVersion 的 applicability 约束；见 plan §Requirement Service、T044/T045。
- CHK007 → 通过。Source/Evidence 的 classification 与 validity 枚举，配合硬约束 SATISFIED/VIOLATED/UNKNOWN 三态，给出“重要事实、过期、合格推荐”的可复核判据；见 data-model、FR-010、T057。
- CHK008 → 通过。Workflow.latest_executable_version_id 只在 validated/confirmed 版本上推进，草稿可保存但不成为最新；见 data-model、T030/T032。
- CHK009 → 通过。“最后可信完成步骤”由检查点协议、SideEffectOperation 核验与来源时效检查共同判定；见 data-model §检查点与恢复协议、T077。
- CHK015 → 通过。spec 新增 §计量口径，定义重要事实、必需栏目、缺口的分母及完整/部分判据。
- CHK016 → 通过。spec 新增假设 14，定义目标用户与初始配置完成判据。
- CHK018 → 通过。ModelRequest 单例非终结占用与 ExecutionSlot 唯一行给出可观测的计数边界；见 data-model、T014。
- CHK036 → 通过。spec §共通约束新增“起点语义”，区分继续、新分支与不可启动节点。
- CHK038 → 通过。UNKNOWN 保持阻塞、不自动解锁，处置经求助决策；见 FR-033、故事 9.12、data-model ModelRequest。

## Notes

- 仅在评审确认需求质量准则满足后勾选；仍需澄清、修正或评估的条目保持未勾选。
- `$speckit-implement` 读取清单状态作为门禁，但不得修改勾选标记；本次已裁定的 9 项视为通过，未裁定的 29 项不阻断一期实施（2026-10-19 决定）。
- `checklists/requirements.md` 是由 `$speckit-specify` 和 `$speckit-clarify` 维护的独立内置清单。
- 2026-10-19 已裁定 9 项（见“裁定记录”），其余 29 项未评定通过或失败；`Gap`、`Ambiguity`、`Conflict` 是审查提示，不是已确认缺陷。
- 可在条目旁补充审查结论、依据及待澄清事项；编号连续以便引用。
- `Spec §用户故事9.3` 表示用户故事 9 的第 3 个验收场景；规划标题引用用于补充设计背景。
