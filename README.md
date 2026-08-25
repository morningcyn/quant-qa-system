# 客服会话质检助手

本地单机运行的客服/投顾会话质检工具：导入聊天记录 → 大模型按 D端(55分)+S端(45分) 规则自动打分 → 生成质检报告（总分、59分黄灯熔断、双雷达图、失分归因、原话 vs AI黄金改写、改进建议）→ 按员工沉淀历史档案（30天趋势、Top3薄弱项）。

**API Key 由使用者在应用内「设置 → 我的模型」配置，使用自己的 Key，绝不烧开发者 token。**

## 快速开始

```powershell
# 1. 安装依赖（一次性）
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python scripts\download_vendor.py   # 下载前端离线依赖（需联网一次）

# 2. 启动桌面应用
.\.venv\Scripts\python run.py

# 开发模式（系统浏览器 + F12 调试）
.\.venv\Scripts\python run.py --dev
```

首次使用：**设置 → 我的模型 → 添加模型**，填入您自己的 API Key（如 DeepSeek `https://api.deepseek.com/v1` + `deepseek-chat`）→ 测试连通 → 员工管理新建员工 → 上传会话质检。

## 打分机制

- 总分 100 = D端 55 + S端 45；**总分 < 59 → 黄灯预警（熔断）**
- D端：D1 情绪转化 10 / D2 画像匹配 15 / D3 诉求穿透 15 / D4 预期超越 15
- S端：S1 情绪维稳 20（共情4+定制5+直接4+无冲突5+宣泄引导2）/ S2 问题闭环 15 / S3 专业供给 10
- 权重与黄灯阈值可在「设置 → 质检模板」调整；**总分与熔断由后端按模板重算**，不信任模型算术；历史报告存模板快照，改模板不影响历史数据

## 数据与安全

- 全部数据保存在本机 `data/app.db`（SQLite），建议定期备份
- API Key 用 Windows DPAPI 加密存储（绑定本机用户+机器），更换电脑/系统账户后需重新填写
- 模型请求直连所选 API 地址（`trust_env=False` 绕过系统代理劫持），日志全链路脱敏

## 支持的文件格式

- 粘贴/文本：`[客] … / [助] …`、`客：… / 助：…` 等角色标记
- CSV：`角色,内容` 两列（兼容 role/content、speaker/text 等表头）
- JSON：`[{"role": "客", "content": "…"}]` 或 `[{"speaker": "customer", "text": "…"}]`

## 开发

```powershell
.\.venv\Scripts\python -m pytest tests\ -q     # 单元测试（mock LLM，无需 Key）
.\.venv\Scripts\python scripts\make_sample.py  # 生成 3 条样例对话（高/中/低分）
```

```
backend\        FastAPI 后端（parser / scoring / prompts / pipeline / llm 客户端 / 统计）
frontend\       手写静态前端（无 Node 构建链；vendor 三件套离线加载）
run.py          pywebview 桌面入口（随机端口 + 后端线程 + js_api bridge）
tests\          pytest 单元测试
```
