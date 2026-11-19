# 霜讯

霜讯从已获授权的基础设施公告来源获取完整快照，保留原始响应，将结构化通告交付给区域保障订阅方。示例与测试均为本地合成演练资料，不代表真实灾情。它不是应急决策系统，通告冲突或来源过期时会撤下当前可用内容，供值班人员核查。

使用 Python 3.11 或更新版本。第三方依赖精确锁定在 requirements.txt。开发环境准备依赖后，运行 python -m unittest discover -s tests -v 执行业务回归；python -m frost.acceptance 启动本地 HTTP 公告站点和 FastAPI 服务，验证真实获取、完整轮次提交、订阅领取、超时重领以及证据导出。

服务通过环境变量 FROST_ADMIN_KEY 读取至少16字符的管理密钥；FROST_DATABASE 指定 SQLite 文件，默认 state/frost.sqlite3。运行 python -m frost serve 启动本地管理接口，默认127.0.0.1:8000。管理操作使用 X-Frost-Key 请求头，交互式接口说明位于 /docs。仅 /health 不要求密钥。请通过受控网络或网关提供管理服务，不要把密钥放入来源URL。

采集调度由 python -m frost worker 启动，使用同一数据库；--once 执行一批到期来源后退出。服务进程不自动开启调度线程。多个调度进程使用数据库租约竞争同一来源，旧租约不能提交新结果。采集中断后等待租约到期会重新执行完整轮次，不拼接不同时刻的半轮结果。

## 来源契约

POST /sources 登记 id、url、format、authority，可指定 priority、interval、stale_after、max_pages、max_bytes。数值边界可见接口及错误响应；默认采集周期60秒、可信时长3600秒、最多30页、每页1MiB。url必须属于有权采集的公开HTTP来源；默认拒绝内部、回环、保留地址，HTTP跳转和分页只能留在该来源原点。验收进程单独明确允许本地回环，不影响正常服务默认限制。

JSON来源每页包含 snapshot、total、items、next。snapshot与total在一轮所有页面保持相同，total表示去重后的来源编号总数；next为同源下一页或null。分页允许相同内容重叠，重复编号内容不同则拒绝本轮。首页面的ETag及Last-Modified用于下次条件请求，来源应保证首页面验证器覆盖整个快照；304表示完整快照仍然有效。分页304和没有完整基线的304均不能提交。完整空快照可以撤下该来源全部当前观察，抓取失败不会当作空快照。

每条通告包含 id、canonical_id、revision、title、body、issued、expires、state、level、regions、facilities。id是来源内稳定编号，canonical_id是授权来源共同约定的通告身份，不做名称相似归并。revision为正整数，同修订必须语义相同，不允许回退或重用编号指向另一身份。issued和expires使用带时区ISO8601，失效时间晚于发布时间；当前版本采到即参与裁决，issued只用于来源间新旧比较。state为active或cancelled，level为info、yellow、orange、red；设施为road、power、water、heat、telecom。区域使用来源与订阅方约定的非空字符串列表。

HTML来源在meta的name=snapshot及name=total声明快照。每个article以data-id、data-canonical_id、data-revision、data-issued、data-expires、data-state、data-level声明属性，data-regions与data-facilities为逗号分隔列表；标题、正文位于data-field=title和data-field=body节点。rel=next链接表示下一页。仅抽取这些明确节点，不推测任意网页的语义。

Atom来源使用标准Atom命名空间，扩展命名空间urn:frost:bulletins:1。feed下扩展snapshot和total表示整轮信息；entry下id、title、content与updated使用Atom元素，扩展canonical-id、revision、expires、state、level表示通告属性。category的scheme为urn:frost:region或urn:frost:facility，term承载区域或设施。正文只接受内联纯文本，XML不接受实体或DOCTYPE。三种来源均只支持UTF-8和GB18030，不执行页面脚本、不下载附件。

## 通告和订阅

只有完整抓取和字段校验通过的轮次会改变观察事实。有效来源按priority优先，再按issued选最新；最高优先级和发布时间都相同但内容不同会显示conflict。相同内容的多个来源保留各自证据而不重复生成业务变更。来源被禁用、过期或通告自然失效时重新裁决，可回退到仍然有效的其他来源。没有可用来源显示unavailable，取消通告显示cancelled。GET /bulletins、/changes及通告的/evidence与/explanation提供当前结果、变更序列和裁决依据。

POST /subscriptions登记id、regions、facilities、min_level。区域和设施分别按交集匹配，级别达到阈值才交付。新订阅获得当前匹配通告。以后通告变更进入或离开订阅范围，会形成upsert或withdraw消息；离开范围的撤回不意味着全局通告取消。订阅的claim返回消息id、租约token和payload，ack确认成功，nack指定延时后重试。消息至少交付一次，收件方应用id去重，同一通告的较早版本确认前不会领取较新版本；不同通告可并行交付。超时重领更换token，旧token不再有效；同token重复ack幂等。

GET /runs/{id}/bundle导出包含原始字节和清单的ZIP，单轮原始字节预算16MiB，超过时逐份读取captures。python -m frost export RUN_ID DESTINATION保存证据且不覆盖已有文件；python -m frost verify-bundle PATH验证离线字节校验。证据校验用于发现损坏，不表示来源真实性的数字签名。operations接口和python -m frost check检查数据库引用及消息版本，无法代替实际来源授权审查。

## 代码组织

frost/acquire承担网络获取、来源调度与轮次完整性；frost/bulletins承担结构化抽取及权威裁决；frost/relay承担订阅范围和交付顺序；ledger保存事务状态及原始证据；operations提供业务解释。tests中的采集、通告交付与格式接口测试分别验证这些边界，acceptance通过本地HTTP联合验证它们。
