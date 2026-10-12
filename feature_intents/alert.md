# alert

## 模块职责

`alert.py` —— 一个 macOS 小工具，干三件事：**放音乐、发通知、调音量**。
音量由调用方决定（CLI 参数 / Python 函数参数）。纯 stdlib，不装任何东西。

它存在的意义：`keep_session daemon` 的「分心看门狗」要到第 3 次违规时
「把音量拉到 10% 并播放 `data/audio/赵小六嘶吼.mp3`」（见
[`idle_watchdog.md`](./idle_watchdog.md)）。把「怎么出声」留在 daemon 里，
会让 daemon 跟 macOS 细节缠在一起、测试也没法不开声音；所以拆成独立脚本，
daemon 只负责决定「什么时候响」。

## 命令

```bash
# 放音乐：--volume 是播放增益 0-100；--system-volume 先把系统音量拧到 N% 再放
python3 alert.py play data/audio/赵小六嘶吼.mp3 --volume 10
python3 alert.py play data/audio/赵小六嘶吼.mp3 --system-volume 10        # 把音量拉到 10% 再放
python3 alert.py play long.mp3 --volume 60 --background                  # 不等待播完

# 发通知：--sound 可选（macOS 声音名，如 Basso）；--system-volume 先拧音量
python3 alert.py notify -t "Focusmate 分心警告" -m "当前违规次数：1/3"
python3 alert.py notify -m "叮" -s Basso
python3 alert.py notify -m "大声点" --system-volume 80

# 只调音量
python3 alert.py volume 10

# 只打印要跑的命令，不出声、不弹通知、不改系统音量
python3 alert.py play x.mp3 --system-volume 10 --dry-run

# 自检 / 演练：音量设到 10% + 播放赵小六嘶吼 5 秒（只放 5 秒，不是完整 24 秒）
python3 alert.py dry-run
python3 alert.py dry-run --seconds 2 --volume 10      # 改时长 / 音量
python3 alert.py dry-run --print-only                 # 真的只打印，不出声、不改音量
```

### `dry-run` 是什么

它是 daemon 第 3 次违规那条链路的**有界演练**：真的 `set volume output volume 10`
+ `afplay -v 1.0 -t 5 赵小六嘶吼.mp3`。用 `afplay -t TIME` 把播放限制在 5 秒，
所以不会真的放完整 24 秒；`--print-only` 才是纯预览（打印命令、零副作用）。
（名字叫 dry-run 有点误导，但它是**会出声**的试跑；默认 5 秒就是为此。）

| 选项 | 默认 | 说明 |
|---|---|---|
| `--audio FILE` | `data/audio/赵小六嘶吼.mp3` | 放哪个文件 |
| `--volume N` | 10 | 系统输出音量（%），会改动 |
| `--seconds N` | 5 | 播放几秒（`afplay -t`） |
| `--print-only` | 关 | 只打印命令，不出声/不改音量 |
| `--background` | 关 | 不等待播放结束 |

## 音量语义（两个别混）

| 参数 | 含义 | 实现 | 副作用 |
|---|---|---|---|
| `--volume N` | **播放增益** | `afplay -v N/100` | 只影响这次播放，不动系统音量 |
| `--system-volume N` | **系统输出音量** | `osascript -e "set volume output volume N"` 再播放 | 改系统音量、且**不还原** |

daemon 的「把音量拉到 10%」用的是后者（字面意思）；想安静点只调 `--volume`
即可。`notify` 只有 `--system-volume` —— 通知没有独立音量，系统音量就是唯一旋钮。

## 实现备忘

- 播放：`/usr/bin/afplay`；默认等播完，`--background` 用 `Popen` 不等（daemon 用，
  免得一段 24s 的嘶吼卡住主循环）。
- 系统音量 / 通知：`/usr/bin/osascript`。
- **中文通知的坑**：`display notification %s` 里的字符串必须用
  `json.dumps(..., ensure_ascii=False)` 转义。默认 `ensure_ascii=True` 会把中文
  变成 `\u4f60...`，而 **AppleScript 不认 `\uXXXX`**，osascript 直接语法错、
  通知根本弹不出来。`keep_session.notify()` 里同款修法。
- 非 macOS、系统工具缺失、执行失败：**静默降级**（打一行 stderr、返回 0/1），
  告警工具不该把调用方（daemon）搞挂。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（非 macOS 跳过也算 0） |
| 1 | 真执行了但失败 |
| 2 | 参数错（音量越界等） |

## 测试

`python3 test/test_alert.py` —— 只断言「会拼出哪些命令」，不真放声音。
覆盖：增益/系统音量换算、越界、中文不转义、`--dry-run`、`dry-run` 演练命令拼装、
非 macOS 降级。
