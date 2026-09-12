# 前端风格统一重构 + 顶级导航架构(已完成)

目标:以研读工作台(`components/reader/`)为唯一视觉基准,完整重构全站前端;引入顶级左侧导航(可折叠,默认窄轨)承载子功能入口(对话 / 阅研 / 文件中心),新增文件中心(下载论文集=网络论文元数据集 + 个人文件)与阅研全站入口两个新页面,并补齐配套后端 API。

## 已确认决定

1. 移除深色模式,全站固定浅色纸感(暖纸 #f1f1ec + 苔绿 #476d3a + 赭石 #b77837)。
2. 覆盖全部页面(含 admin,admin 保持独立信息架构仅统一风格)。
3. 完全对齐工作台密度:13px 基准、正文 11-12px、圆角 5-9px、紧凑间距。
4. 按钮布局统一为工作台样式(`.btn-primary/.btn-secondary/.btn-ghost` 语义类)。
5. 顶级导航 = 可折叠宽侧栏:默认折叠 64px 图标轨,展开 200px 显示名称;文件中心展开显示子项(下载论文集/个人文件);折叠状态 localStorage(`paper-agent-rail-open`)。
6. 顶级导航覆盖所有用户页;login 独立;工作台保持全屏沉浸。
7. 下载论文集 = 跨会话网络论文元数据聚合(不重启 PDF 下载)。
8. 同一论文从任何入口打开复用同一 reading session(进度/笔记保留,互不干扰)。
9. 清小搭 `/v1` 通道与 `data/openai_api/` 零改动。

## 完成内容(2026-09-05)

### 后端
- `GET /api/v1/chat/files?kind=attachment|export`:按 owner 列附件(`core/web_artifact_store.py::list_web_artifacts` 新增),含磁盘大小与 created_at。
- `GET /chat/file/{id}/raw` 放开文档原件下载:图片 inline,文档 `attachment` + RFC 5987 中文名;扩展名白名单;local legacy 注册冲突改为 404(`_legacy_local_metadata`)。
- `GET /api/v1/papers`(`backend/app/api/v1/papers.py` 新路由):按 DOI/id/标题去重聚合 papers+candidates,摘要截断 600,关联会话≤5,owner 隔离。
- `POST /reader/open` 幂等:不传 history_filename 时复用该附件最近所在会话(`_latest_session_with_attachment`),无则建草稿。
- 测试 `tests/test_file_center.py` 9 例;全量 pytest 871 passed。

### 前端
- `app/globals.css` 全量重写:「阅研纸感」令牌(映射见 git 历史),删 `.dark`;语义类收紧(card 9px、btn-primary 橄榄绿 7px/11px、badge 4px、input 7px/12px、chat-prose 13px/1.9、usage-document-prose 13px);新增 `.btn-secondary/.btn-ghost`;focus 环 #438579。
- 深色模式链路删除:layout 内联脚本、chat store theme/setTheme、SettingsPopover 主题块、i18n settings_theme*、两个 e2e 脚本断言反转。
- `components/AppRail.tsx`(Suspense+useSearchParams 处理 tab 高亮)、`AppFrame.tsx`;AppShell 去 Nav 挂 AppRail;Nav.tsx 删除;usage-doc/feedback 套 AppFrame;Sidebar 去品牌区、密度收紧。
- 新页:`app/reader/page.tsx`(我可读 PDF 列表→openReader)、`app/files/page.tsx`(papers/uploads 双 tab,URL ?tab= 寻址);`lib/files-api.ts`、`lib/papers-api.ts`。
- OpenReaderButton 放开"会话未保存"限制(open 幂等后安全)。
- 全站硬编码色清理(red-500/amber-500/emerald-500/bg-black/60 → error/warning/success/bg-fg/40);GenealogyGraph 簇色改苔绿-赭石系(首色 #476d3a);全站 rounded-xl/2xl→[9px]/[7px]、text-sm→12px、text-xs→11px 批量对齐。
- `reader.css` 反向令牌化 9 处(bg/fg/muted/line/surface/accent/focus-ring 消费全局 var),视觉零变化。

### 文档
- README:功能列表加顶级导航/文件中心/阅研入口,反馈入口描述更新。
- docs/DESIGN.md:目录结构、§11 设计段(阅研纸感/AppRail/文件中心 API)、§11.1 open 幂等语义、Nav 引用清理。

## 结构精修(2026-09-06)

- 新增 `components/WorkbenchUI.tsx`:统一页面标题、工作台面板、状态提示、空状态、分页签与操作栏,并被阅研、文件中心、使用文档、反馈、登录页及管理后台复用。
- AppRail 窄屏展开改为抽屉+遮罩,路径和文件中心 `?tab=` 变化后自动收起;移动端 hydrate 默认关闭聊天两侧栏,避免内容被挤压。
- `/chat`、`ChatInput`、`ChatMessage` 与 `/login` 收紧欢迎态、消息标记、输入框和认证面板的层级与圆角;管理后台补充 setting row、notice、table、统一保存栏与 Escape 关闭弹窗,移除渐变标记。
- 强化 `e2e-hydration.cjs`:后端未启动造成的 `/api/v1` 资源错误单独记录,真实 hydration/runtime 错误会使脚本失败。

## 验证记录

- pytest 全量:871 passed, 1 skipped(既有)。
- `pnpm lint`(仅 1 个既有 img 警告)、`pnpm build` 18 页全部通过。
- `settings_check.cjs` 通过;强化后的 `e2e-hydration.cjs` 无 hydration/runtime 错误(后端未启动的 API 资源错误按预期单独记录)。
- 移动端浏览器 smoke 覆盖 `/chat → /reader` 以及 `/files?tab=papers ↔ /files?tab=uploads`,抽屉均正确收起;此前的 9 页截图、数据隔离和工作台零回归记录仍有效。

## 已知残留 / 后续可选

- admin 仍保留各页面原有业务字段与表格布局,共享壳层与设置行已统一;如需更强的逐页信息架构调整可另立任务。
- `/chat` 页右栏"工作台"tab 与 `/reader` 入口功能重叠,保留两者(前者会话作用域,后者全局),后续可考虑合并。
