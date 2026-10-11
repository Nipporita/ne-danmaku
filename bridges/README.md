# QQ 官方机器人 → 弹幕姬 桥接器

把 **QQ 开放平台官方机器人**推送的群消息，转成弹幕姬认识的 **OneBot v11** 事件，让群消息上屏。

```
QQ 群 ──官方 WebSocket(全量模式)──▶ qqbot_official_bridge.py ──OneBot v11 反向WS──▶ 弹幕姬 ──▶ OBS
```

## 为什么用官方机器人，而不是 LLBot / NapCat

| | 官方机器人（本目录） | 第三方协议端 |
|---|---|---|
| 合规性 | ✅ 官方通道 | ❌ [运营规范](https://bot.qq.com/wiki/business/) §5.4 明确禁止「使用插件、外挂或其他违规第三方工具、服务接入本服务和相关系统」 |
| 封号风险 | 无 | 有（无头模式官方明说「有掉线风险，号体质不佳的慎用」） |
| 需要的账号 | QQ 开放平台的 AppID / AppSecret | 第三方签名服务账号（auth.luckylillia.com）+ 官方 QQ 客户端（有头模式） |
| 服务器内存 | ≈ +0.15 GB（就这一个进程） | +0.3 GB（无头）～ +1.2 GB（有头，含 QQ 客户端） |
| 许可证 | 无第三方许可问题 | LLBot GPL-2.0 / NapCat 限制性许可，且有闭源 `.node` 模块 |
| 代价 | 只有 OpenID（无真实 QQ 号）、不能主动发消息 | 无（但上面那些风险都要担） |

## 一、申请与开通

1. **注册开放平台**：<https://q.qq.com/> → 主体认证（个人入口即可，需身份证实名；企业需营业执照+对公打款验证）
2. **创建机器人**：填昵称/头像/简介 → 拿到 **AppID** 与 **AppSecret**（在「开发设置」里）
3. **服务范围**（决定能进哪些群）：
   - **未认证**：仅管理员使用，**可添加到"管理员作为群主"的群** ← 社团自己的群通常就够
   - **个人认证**：可公开使用，进群上限 500
   - **企业认证**：全场景公开
4. **事件订阅方式**：选 **WebSocket**（"不依赖公网服务器部署，适合个人单机使用"）——**内网服务器/笔记本都能用，不需要公网回调地址**
5. **把机器人拉进群**（每个要上屏的群都要）
6. **⚠️ 每个群单独开启全量接收**：由**群管理员在群里打开机器人资料页 → 开启通知/接收所有消息**。
   开启时机器人会收到 `GROUP_MSG_RECEIVE` 事件，本程序会打印：

   ```
   ★ 群 XXXX 已开启「接收所有消息」（全量模式），群消息会上屏：群号 278699690，房间「弹幕群」
   ```

   （关闭时对应 `GROUP_MSG_REJECT`，同样会提示。）
7. 可选：在「开发设置」里配置 **IP 白名单**（不配则允许所有来源）、**内部体验号码**（最多 20 个，彩排用，不受服务范围限制）

## 二、配置

```bash
cd bridges
cp qqbot_bridge.example.json qqbot_bridge.json
vi qqbot_bridge.json
```

关键字段：

| 字段 | 说明 |
|---|---|
| `app_id` / `client_secret` | 开放平台的 AppID / AppSecret（**别外传**，`qqbot_bridge.json` 已在 `.gitignore` 里） |
| `sandbox` | `true` 走沙箱环境（`sandbox.api.sgroup.qq.com`） |
| `danmaku.url` | 弹幕姬 OneBot v11 入口，默认 `ws://127.0.0.1:5701/ws` |
| `danmaku.access_token` | **必须与弹幕姬 `config.json` 的 `onebot_v11.access_token` 完全一致** |
| `danmaku.self_id` | 随便一个数字，作为机器人号标识 |
| `groups` | `group_openid` → `{group_id, room}`；`group_id` 用你的**真实群号**（这样弹幕姬原有的 `group_map` 就能直接用） |
| `blacklist_file` | 相对本配置文件的路径，默认复用弹幕姬的 `assets_danmaku/blacklist.txt`（约 706 条正则） |
| `max_message_length` | 单条弹幕最大字数 |
| `forward_images` | 是否把图片消息当表情上屏（弹幕姬只认"单张图片"的消息） |

**怎么拿到 `group_openid`**：第一次有群消息上来时，程序会打印

```
收到未配置群的消息（group_openid=XXXX）。请把它填进 qqbot_bridge.json 的 groups …
```

照抄进去，`group_id` 填该群的真实群号即可（不填真实群号也行，程序会生成一个稳定的 9 位数字，但那样还要把这个数字加到弹幕姬的 `group_map` 里，所以**建议直接填真实群号**）。

## 三、弹幕姬侧要改的地方

`danmaku/config.json`：

```json
{
  "danmaku": {
    "onebot_v11": {
      "host": "127.0.0.1",
      "port": 5701,
      "access_token": "与桥接器一致",
      "secret": null,
      "group_map": { "278699690": "弹幕群" }
    }
  }
}
```

- 原来是 `satori`（对接 LLBot）的，改成 `onebot_v11`；也可以两个都留着，只有实际连上的源会推消息。
- `group_map` 用**真实群号 → 房间名**，与桥接器 `groups[].group_id` 对应。

## 四、运行

```bash
# 前台运行（调试）
python qqbot_official_bridge.py --config qqbot_bridge.json

# 自测：不连官方网关，注入内置样例事件，验证与弹幕姬的链路
python qqbot_official_bridge.py --config qqbot_bridge.json --mock
```

**自测已验证的行为**（用弹幕姬真实进程跑过）：

- 普通文本消息上屏，昵称正确显示
- `/sc 10 测试` 被正确解析并**扣费**（余额 10 → 0）
- 未配置群会打印 openid 提示
- `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT` 会提示哪个群开/关了全量接收

systemd 单元示例：

```ini
[Unit]
Description=QQ Bot -> Danmaku bridge
After=network.target

[Service]
User=yuanhuo
WorkingDirectory=/opt/yuanhuo/danmaku/bridges
ExecStart=/opt/yuanhuo/danmaku/.venv/bin/python qqbot_official_bridge.py
Restart=always
RestartSec=5
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=multi-user.target
```

## 五、出网与网络

- 只需要**出网**到：`api.bot.qq.com`（取 access_token）、`api.sgroup.qq.com`（网关与 OpenAPI）、`q1.qlogo.cn`（头像，可选）
- **不需要公网入站**：WebSocket 是出站长连接 ✔
- access_token 有效期 7200 秒，程序会自动刷新；请勿把 AppSecret 放到前端

## 六、已知限制（都是官方通道的固有约束）

| 限制 | 说明 / 应对 |
|---|---|
| 只有 OpenID | 拿不到真实 QQ 号，所以弹幕身份是 `member_openid`；群映射需要手工配一次（见上） |
| 不能主动发消息 | 官方限制被动回复（需 msg_id 且有时限）→ 弹幕上屏不受影响，但「机器人在群里回执」做不了 |
| 头像不可用 | OpenID 拼不出有效头像 URL。**已在弹幕姬 `onebot_v11_client.py` 打补丁**：只有纯数字 QQ 号才去拉头像，避免每条消息白等最多 10 秒 |
| 内容安全 | 运营规范 §5.1 要求过滤机制：已复用弹幕姬 706 条黑名单；如需更强可接官方内容安全检测接口 |
| 数据删除义务 | 规范要求"用户要求删除需删除""停止使用后清除数据" → 需要时删 `cash.db` / `room.db` 里对应用户记录 |
| 全量接收需群管理员逐个群开启 | 见 §一.6；彩排前务必确认每个群都开了，否则该群不上屏 |

## 七、故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 桥接器日志 `server rejected WebSocket connection: HTTP 400` | 弹幕姬用 aiocqhttp，**握手必须有 `X-Client-Role` 头**（本程序已带 `X-Client-Role: Event`）。若你自行改造连接，别忘了它 |
| `401/403` | `danmaku.access_token` 与弹幕姬配置不一致 |
| 弹幕姬收到消息但不上屏 | 该群号不在 `onebot_v11.group_map` 里（弹幕姬会静默忽略） |
| 群里发消息没反应 | ① 群是否已开"接收所有消息" ② 桥接器是否打印了 openid 未配置提示 ③ 机器人是否在群里 |
| 取 token 失败 `100016` | AppID / AppSecret 不对；`100007` 表示 AppID 无效或机器人被禁用 |
| 弹幕延迟大 | 关闭 `forward_images`，或检查 `q1.qlogo.cn` 是否可达（见 §六 头像条目） |
