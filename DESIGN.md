# 按任务类型批量整理、匹配与提交

用户选择任务类型并上传一批资料，系统根据文件名、正文、可核实的客户编号、证件号码或业务记录ID自动匹配当前用户的待办记录，将属于同一记录的资料合并，构造该业务接口接受的payload并提交。用户不需要逐条选择审批记录。歧义、缺件、冲突或接口条件无法确定的对象直接跳过。

助手和AML是独立服务，通过HTTP接口、嵌入页面和postMessage扩展业务能力，不共享源码、目录或数据库。

## 操作入口

顶部「上传资料」打开批量对话框：选择类型、批量选择文件或ZIP，再点击「上传、匹配并提交确定项」。处理结果展示目标任务、业务记录、实际提交内容、接口结果和来源。历史批次保留在对话框内；本批上传清单与逐项处理回执同时保存在当前对话，刷新、切换会话或重启后可查询。

「我的待办」用于查看和筛选记录，不再放置“整理资料并预填”逐条操作按钮。旧的补材料工作流接口保留兼容历史任务，不作为本功能入口。宿主传入taskContext时，仅用于选择任务类型，仍从整批资料自动匹配记录。

## 处理边界

模型逐份提取对象、标识和材料意见，服务端核验source_id、原文引用、Unicode字符偏移和SHA-256。文件名只能作为归属线索，不能单独生成材料事实。模型不直接选择任意任务ID或调用写入工具。

服务端扫描当前用户完整待办，最多5000条；不完整分页不匹配。所有提供的编号必须共同匹配同一条记录。仅有名称、同一客户多条待办、编号冲突、名称不能交叉核实、组合客户风险任务均不自动处理。每条记录的多份资料再次合并核查，缺件、过期、疑问或冲突时跳过，不用置信度阈值代替确定依据。

ZIP保存整包原件，并将成员分别保存、识别及关联；不把整包文件挂到每条客户记录。单个Excel、PDF或文本含多个对象时，只有各对象的正文依据能隔离且不重叠，才生成该记录的UTF-8摘录附件；引用仍回到原件和提取正文。不能隔离时跳过。

当前支持PDF、DOCX、XLSX、TXT、CSV、JSON、Markdown、ZIP；PNG/JPEG仅存原件，无OCR正文时不参与自动提交。合法零宽、组合和特殊字符保留；非法编码、孤立代理字符、NUL等保存原件及读取错误，跳过自动处理。单原件10MiB，每批最多200个来源、合计50MiB；ZIP展开最多100项、12MiB、两层嵌套，成员不落盘、不执行。

## 当前业务写入契约

| 类型 | 已核实的写入路径及适用范围 |
| --- | --- |
| risk | `aresoft/act/processComplete`，只接受单客户、等级与详情一致、无变更/受限/人工修改、唯一通过路径的任务；意见和附件来自匹配资料 |
| bnfValidate | `custandbnfinfo/bnfReport/businessFirstDifferenceHandle`，当前仅接入初审“确认无差异”(10)，必须有核验记录ID及明确材料；其他差异分支、复审外部报送需要额外表单契约，直接跳过 |

按本次业务要求，目前调用现有推进流程接口，确实可能推进审批。后续AML增加“仅保存”接口时，配置`AML_BATCH_WRITE_PATHS`即可替换地址，payload不变，例如：

```bash
AML_BATCH_WRITE_PATHS='{"risk":"assistant/prefill/risk","bnfValidate":"assistant/prefill/bnf"}'
```

上述预填地址是待AML实现的契约示例，不是当前已存在的接口。当前默认使用表中真实推进接口。请求头`X-Assist-Submission-Id`提供助手提交台账ID；AML可保存这个关联ID，反查完整payload和资料来源。原业务接口返回成功后助手标记submitted；不推断外部异步报送已完成。

提交前重新验证按钮权限、待办归属、客户和流程条件。请求完整payload先写入`batch_submissions`，然后通过现有提交幂等表按任务占位。写入不自动重试；超时记uncertain；进程在提交中退出会保留submitting及请求快照。批次重复请求不重放；必须先在AML核对实际结果。

## 数据与反查接口

`material_batches`保存类型、owner、会话和批次状态；`batch_sources`保存不可变原件、提取正文、父ZIP及成员路径、哈希和识别结果；`batch_submissions`保存目标任务/业务记录、标识匹配依据、字段来源、实际路径、payload、接口结果及错误状态。字段原文引用与用户后续修改应分别保留，修改值不改变原引用的含义。

| 接口 | 用途 |
| --- | --- |
| POST /api/material-batches | 指定process_type及可选conversation_id创建批次，无需task_id |
| POST /api/material-batches/{id}/files | 上传批次资料，ZIP成员分别入库 |
| POST /api/material-batches/{id}/process | 自动识别、匹配、提交确定项，持久化回执 |
| GET /api/material-batches、/{id} | 本人批次历史、来源及逐项完整结果 |
| GET /api/records/{type}/{act_id}/material-submissions | AML按业务记录反查完整请求和来源 |
| GET /api/batch-sources/{id}、/{id}/original | 来源正文及不可变原件 |

反查默认只返回当前上传者自己的历史。后续审核人传入`task_id=当前审核任务`时，助手实时核实此用户拥有同一业务记录的当前待办，才允许读取其他上传者的提交结果。读取这些来源还需传`submission_id`和`current_task_id`，再次核对来源属于该记录，且审核人仍有权限。跨用户不能通过猜测source_id、批次ID或提交ID读资料。所有接口均使用X-Access-Token，不在URL传token。

宿主集成示例：

```javascript
const headers = {'X-Access-Token': currentSessionToken};
const response = await fetch(
  assistantBase + '/api/records/risk/' + encodeURIComponent(businessId) +
  '/material-submissions?task_id=' + encodeURIComponent(currentTaskId), {headers});
const submissions = (await response.json()).data.records;
// 每项data包含payload、field_provenance、匹配依据及result；宿主展示并允许人工修改业务字段。
```

对话中逐项回执保存完整快照，模型可通过只读工具`get_material_batches(batch_id)`查询本人批次的实际结果。普通聊天模型没有提交工具。

## 数据库与部署

升级数据库执行[批量资料迁移](sql/V0.9.0__material_batches.sql)，新库使用`sql/mysql8-init.sql`。与已有配置表机制一致，首次访问批量接口可补建缺失的三张新表；运行账号无DDL权限时会明确提示执行迁移。应用不修改AML数据库。

新增API需要重启助手Python服务；前端静态页面刷新后加载。若旧进程还未重启，上传弹窗会提示批量接口尚未加载。模型和AML可用性需要目标环境联调；测试中的外部写入使用假接口，不对真实待办做提交试验。


## 资料提交记录与完整Trace

顶部「资料提交记录」独立展示本人所有批次，支持任务类型、批次状态筛选及分页。每批显示来源数、成功/跳过/结果不明数，明细包含实际payload、接口结果、原文引用及来源原件；可跳转对应会话、本条处理Trace或整批Trace。

创建、上传、处理三个POST入口生成独立Trace，并通过`batch_trace_links`关联同一批次。`GET /api/material-batches/{id}/traces`返回阶段列表及已保存请求的累计耗时、模型耗时、调用数和Token。查询、轮询、配置和心跳不新增Trace。链接表按批次、owner、时间索引；查询严格按用户权限隔离。

批量入口的意图来自用户选择的任务类型与提交动作，不伪造一次模型意图推理。业务工具由编排器调用，明确标记selection_source=orchestrator；聊天工具的模型选择则记录实际tool_calls、参数和关联run_id。模型每次结构化识别及合并均连接LangChain回调，保存实际输入、原始输出、结构化结果、可选schema/工具、推理摘要（若返回）、提供方用量、耗时及错误。原文校验、匹配、提交条件核验、附件上传、payload构造、提交快照落盘、业务HTTP请求与回执保存均有事件及耗时，不能只看最终模型摘要。

`batch_submissions.data.trace_id`和对话回执关联处理Trace；完整业务payload仍保存在提交台账。Trace保存有配置的单字段和事件数量上限，页面标记截断，不把Trace当成原件存储。模型未报告Token时标记usage_complete=false，仅累加已报告用量；未报告推理时明确显示缺失。阶段耗时会嵌套，不能简单相加当作请求总耗时；批次请求累计耗时不包含用户选文件、读页面的等待。

已有环境执行`sql/V0.9.1__batch_traces.sql`，有DDL权限时首次使用可自动补建关联表。重启服务后新提交才会产生新增观测；旧批次缺失的模型输出、Token和历史Trace不能补造。Trace按留存策略过期后，关联列表会标记无已保存详情，提交台账与来源仍可查询。
