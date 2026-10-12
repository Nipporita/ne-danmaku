# Nekocast - Danmaku Core

> 元火弹幕姬核心

<span style="text-decoration:line-through">⚠️这个项目完全使用 Claude Code / Codex 生成，未经任何人工修改</span>  

## 使用

你需要 [`uv`](https://docs.astral.sh/uv) + `node` + `pnpm` 来运行这个项目

### 构建前端部分：

```bash
cd frontend
pnpm install
pnpm build
```

### 搭建 Satori 弹幕源

由于 gugugaga bot 的日新月异，我们只尝试了 [LLBot](https://www.llonebot.com/guide/getting-started) 的 Satori 服务作为 gugugaga 弹幕源。

请配置后在 config.json 中填写 Satori 相关配置。

### 搭建 B 站弹幕源

请从 bilibili 直播间获取房号填写在 config.json 中。

SESS_DATA 为可选，如果需要查看具体观众昵称请从 cookie 获取后填写。

### 搭建 OneBot v11（aiocqhttp 反向 WS）弹幕源

`onebot_v11` 配置用于在本服务内启动一个反向 WebSocket 事件入口。

请在 OneBot v11 实现（如 LLOneBot / NapCat）中将反向 WS 地址指向：

- `ws://<本服务IP>:<onebot_v11.port>/ws`

并在 `group_map` 中将 QQ 群号映射到弹幕房间名。

若 OneBot 侧配置了访问令牌或签名密钥，请同步填写 `access_token` / `secret`。

### SC 与礼物动态配置

弹幕子模块支持通过 `config.json` 动态配置文本指令里的 SC 与礼物行为。

- `/sc` 文本指令格式：`/sc [cost] 文本`
- 若省略 `cost`，使用 `danmaku.superchat.default_cost`
- SC 展示时长由金额自动计算，不再与金额脱钩：
	`duration = clamp(cost * duration_per_cost, min_duration, max_duration)`

配置示例（节选）：

```json
{
	"danmaku": {
		"superchat": {
			"default_cost": 10,
			"duration_per_cost": 1,
			"min_duration": 10,
			"max_duration": 300
		},
		"gift": {
			"default_cost": 1,
			"items": {
				"good": {
					"cost": 1,
					"aliases": ["这个好诶"],
					"image_url": "/assets/gifts/good.png"
				},
				"tower": {
					"cost": 5,
					"aliases": ["博雅塔"],
					"image_url": "/assets/gifts/tower.png"
				}
			}
		}
	}
}
```

- `/gift` 文本指令格式：`/gift 礼物名 [数量]`
- 礼物单价优先使用 `gift.items` 中命中的键名或别名；未命中时使用 `gift.default_cost`
- 礼物总价计算：`cost = 单价 * quantity`
- 礼物图片可通过 `gift.items.<key>.image_url` 配置，支持绝对 URL 或站内路径（如 `/assets/gifts/good.png`）

### 发言奖励与现金系统

对于 OneBot v11 和 Satori 弹幕源，系统内置了一套虚拟货币（火）机制，用于控制文本指令 `/sc`、`/gift` 的消费校验。

**奖励策略（可叠加）：**

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 是否启用现金系统；禁用时允许无限发送礼物/SC |
| `initial_amount` | float | `10.0` | 用户首次发言时获得的初始火量 |
| `reward_per_message` | float | `0.0` | 每条有效消息奖励的火量 |
| `reward_interval_seconds` | int | `0` | 间隔奖励周期（秒），0 表示禁用 |
| `reward_per_interval` | float | `0.0` | 每个周期额外奖励的火量 |

**配置示例（节选）：**

```json
{
  "danmaku": {
    "cash": {
      "enabled": true,
      "initial_amount": 10.0,
      "reward_per_message": 0.5,
      "reward_interval_seconds": 60,
      "reward_per_interval": 1.0
    }
  }
}
```

用户首次发言获得 10 火，之后每条消息额外获得 0.5 火；同时每隔 60 秒再额外获得 1 火。

**消费校验：** 发送 `/sc` 或 `/gift` 时会先检查余额是否足够，不足则该指令被拒绝（不会发送到弹幕流）。

### 运行服务：

将 config.example.json 复制为 config.json 并修改配置后运行

```bash
uv run main.py
```

服务会在对应的端口启动 HTTP 服务

- `/danmaku/房间名` 为弹幕网页，管理、咕咕嘎嘎、B 站弹幕均会显示在此页面，可以直接添加到 OBS

## 屏蔽词库

两级来源，配置里分开写：

| 配置项 | 内容 | 匹配方式 |
| --- | --- | --- |
| `blacklist_file` | 自己的规则（默认 `assets_danmaku/blacklist.txt`） | **正则**，管理端可以现场增删并写回该文件 |
| `blacklist_extra_files` | 第三方开源词库（见下） | **字面量**（词表不是正则），只读，不会被写回 |

`blacklist_extra_files` **不用手写**：配置里不写（或写空数组）就自动用
`nekocast_danmaku/config.py` 里的 `DEFAULT_BLACKLIST_EXTRA_FILES` ——
也就是 `blacklist/` 下那套标准词库（11 份：广告 / 色情 / 涉枪涉爆 / 暴恐 / 政治 / 其他 / 补充）。
相对路径按**仓库根目录**解析，不必担心从哪个目录启动；文件不存在只警告、不影响启动。
想只用其中几份、或加自己的词表，就显式写出来（写了就以你写的为准）：

```json
"blacklist_extra_files": ["blacklist/sensitive-stop-words/广告.txt"]
```

### 第三方词库（git submodule）

`blacklist/` 下直接引用了别人的仓库，词表内容**不进本仓库的历史**（只记录 commit 指针）：

| 目录 | 仓库 | 许可 |
| --- | --- | --- |
| `blacklist/Sensitive-lexicon` | <https://github.com/Konsheng/Sensitive-lexicon> | MIT |
| `blacklist/sensitive-stop-words` | <https://github.com/fwwdn/sensitive-stop-words> | Apache-2.0 |

首次克隆、或换机器后要拉取词库：

```bash
git submodule update --init --recursive
# 想更新到上游最新
git submodule update --remote blacklist/Sensitive-lexicon blacklist/sensitive-stop-words
```

词表文件改完/更新完，watchdog 会自动重载（额外词表的目录也在监视范围内），不必重启。

### 为什么额外词表按「字面量」处理

这些词库是给论坛、评论区用的通用文本审核词表。直接当正则用有两个坑：词条里的 `*`、`(`
会变成通配符；单字词条（"草""操"这类）会把大量正常弹幕也标成 `blocked`。
所以加载时会：转义成正则字面量、丢弃长度 < 2 的词、与已有规则去重。

### 误伤体检

引第三方词库必然有误伤（例如某个词库里有 `早苗`，本意匹配"高市早苗"，
但东方活动上"东风谷早苗"才是常态）。体检脚本按**实际生效的规则集**跑一遍：

```bash
python tools/check_blacklist.py          # 需要 .venv 里跑；会指出"这条来自哪个文件"
```

被标 `blocked` 只影响大屏显示（不渲染），**不影响游戏计票/答案**；
但主持人指令、正常弹幕看不见会很难受——把太吵的整份词表从
`blacklist_extra_files` 里去掉即可。

## 行内元素（`<face .../>` 之类）

上游会把"不是纯文字"的段塞进消息文本里，形如：

```
哈哈哈哈<face id="339" type="2" platform="llonebot"/>好活
```

后端（`nekocast_danmaku/danmaku/inline_elements.py`）在**过滤阶段**为每条消息解析它们
（所有上游共用这一步），并给帧补三个字段：

| 字段 | 内容 |
| --- | --- |
| `inline` | 切好的段：`[{"kind":"text","text":"哈哈哈哈"}, {"kind":"face","id":"339","type":"2","url":"…","url_alt":"…"}]` |
| `text_plain` | 元素整体替换成 `█` 之后的纯文本（给游戏这类只认纯文本的消费者；**整条只有元素时就是一个 `█`，照样算数**） |

没有行内元素的消息**不补**这两个字段（帧保持尽量小）。
`platform` 之类的附加属性**不做处理**（只出现在 `attrs` / `raw` 里），渲染只认 `id` 与 `type`。

**表情图地址**：QQ 系统表情在腾讯老地址 `qzonestyle.gtimg.cn/qzone/em/e{id}.gif`，
其中"会动的"那种（`type="2"` 或 id ≥ 222）编号是 `10000+id`（实测 e419 是 404、e10419 才是 200）；
QQ"回应"那类标准 emoji 的 id 直接是 Unicode 码点（128077 = 👍，和贴表情那套一致），走 Twemoji。
每个 face 段都给了 `url` 与 `url_alt` 两个地址，前端一个挂了换另一个，最后退回 `█`。

**前端**（`frontend/src/components/DanmakuDisplay.vue`）：带行内元素的 `plain` 弹幕
用 `danmaku` 库的 `render` 钩子画成"文字 + 图片"的 DOM（和表情包走同一条路；文字用
`textContent` 写入，绝不拼 HTML）。房间关掉外部表情、或图片加载失败 → 显示 `█`。

改完前端记得重新构建（后端挂的是 `frontend/dist`）：`cd frontend && pnpm build`。

- `/danmaku/chat/房间名?key=密钥` 为管理端发送弹幕和动态配置房间设置（透明度、emoji、superchat、gift、置顶/置底定位开关）的界面
