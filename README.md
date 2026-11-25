# njupt-very-smart-class

[English](README.en.md) | 简体中文

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![CI](https://github.com/Ptilopsi4/njupt-very-smart-class/actions/workflows/ci.yml/badge.svg)](https://github.com/Ptilopsi4/njupt-very-smart-class/actions/workflows/ci.yml)

把南邮智慧课堂（[njupt.smartclass.cn](https://njupt.smartclass.cn/)）的课程录播，自动整理成一份**带时间戳的课堂笔记**。
一节 45 分钟的课，大约 10 分钟后得到一个 `summary.md`，模型费用约 ¥0.6。

> 仅供个人学习使用。需要你自己的统一身份认证账号；遵守学校与平台规定，禁止传播含他人声音、肖像的录像和生成的笔记。

## 有什么用

用 `vsc run <编号>` 处理完一节课，你会得到一个 `summary.md`，按这个顺序组织：

- **一句话概览**：这节课讲了什么。
- **章节时间轴**：按时间分成若干章（一节 45 分钟的课通常十几章），每章有起止时间和几条要点。时间从录播开始算起，对着录播拖到对应位置就能核对。
- **核心知识点**：按重要性排好的知识点。
- **课堂演示**：老师在屏幕上敲的命令和输出，按时间排好。
- **问答与互动、作业 / 通知 / 考试提示、术语表**。
- **关键幻灯片**：最多 10 张课件截图，图片直接嵌在笔记里。
- **ASR 勘误**：语音识别听错的术语，自动对照画面上的证据改正（例如把 `echo` 听成了 `apple`）。原词和改正并列，方便你自己判断。
- **待人工复核**：模型拿不准的地方，主要是两种模式说法对不上的条目，会附时间和原因记入产物。

程序同时用两种模式处理，最后合并：

- **转写模式**：把老师说的话逐句转成带时间戳的文字，再让视觉模型描述每一页课件，交给文本模型分段归纳、再汇总。
- **视频模式**：让多模态模型通看整段录像，记下详细的时间轴和屏幕上发生的事。

合并时，章节的时间轴以视频模式为准，老师原话以转写为准，屏幕上的命令和输出以画面为准；两种模式对不上的不强行取舍，列进「待人工复核」。

另外几点：

- **幂等**：每一步做完都会存盘，中断后重跑同一条命令会从没做完的地方接着来。
- **容错设计**：空教室、静音、教室没有屏幕画面、老师自带电脑投屏、分成多段录制、两路画面时长对不上、模型输出被截断。这些情况都会在笔记末尾的「处理提示」里写明哪部分可能缺了。没有转写时（老师没用麦，或转写出错），笔记只能基于画面生成，并给出提示。
- **批量操作**：`--title` / `--semester` 把一门课的录播一次处理完，已完成的自动跳过，单节失败不影响其他。
- **AI friendly**：`--json` 输出结构化结果，退出码固定，附带 skill： `skills/very-smart-class/SKILL.md`。

## 安装

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及 `ffmpeg`（含 `ffprobe`）。

```bash
git clone https://github.com/Ptilopsi4/njupt-very-smart-class
cd njupt-very-smart-class
uv sync
cp .env.example .env      # 填入阿里云百炼的 API Key
```

API Key 在[阿里云百炼控制台](https://bailian.console.aliyun.com/)申请；模型按用量计费，见「费用和时间」。

## 用法

首次使用：

```bash
uv run vsc doctor                                   # 检查环境：ffmpeg、API Key、登录状态、磁盘、模型配置
uv run vsc login                                    # 用统一身份认证登录，按提示输入密码
uv run vsc list --semester current --limit 20       # 列出本学期的录播，新的在前
uv run vsc run 3f9a1c2e                             # 处理一节课：下载 → 整理 → 写出 summary.md
uv run vsc summary 3f9a1c2e --print > notes.md      # 把笔记打印出来，存成文件
```

`run` 后面跟的是录播编号，就是 `vsc list` 输出里第一列 `NewID`（前 8 位就够）。登录会话大约 1 小时过期；已经下载到本地的课，用 `vsc run --local data/<课程名>` 继续处理，不需要再登录。

### 常用场景

| 目标 | 怎么做 |
|---|---|
| 处理一节课 | `vsc run <编号>` |
| 处理一门课 | 先用 `vsc list --semester current --title 编译原理` 看有多少节，再 `vsc run --semester current --title 编译原理 --limit 5` |
| 节约模型费用 |  `--no-a-path`：不看整段视频，只靠转写和课件 |
| 继续处理已下载的课 | `vsc run --local data/<课程名>`，不需要登录 |
| 单独运行特定步骤 |  `--stages transcribe,vision,summarize,render` |
| 查看处理进度 | `vsc status <编号>` |
| 磁盘空间管理 |  `--prune-video`（处理完删掉 mp4 原件）；`--min-free 2GB` 设定下载前的磁盘下限 |
| 更换模型 |  `--model-text`、`--model-vision`、`--model-asr`、`--model-omni`，或者写进 `.env` |

其余选项：`--omni-input Video1`（视频模式换用摄像机画面）、`--omni-sample-seconds`（视频模式每几秒取一帧，默认 5）、`--concurrency`（同时识别多页课件）、`--no-skip-done`（批量时不跳过已完成的课）。全部选项见 `vsc run --help`。

换模型后不用手动清缓存：每一步的结果都附带模型和输入元数据，发生变更自动重算，后面的步骤也跟着重算。批量默认跳过已完成的课时，换模型后要让它们重算需加 `--no-skip-done`。`--force-vision` / `--force-omni` / `--force-fuse` 用于主动重算某一步，它们后面的步骤会一并重算。

### 命令一览

所有命令都支持幂等执行，中断后重跑同一条命令即可。

| 命令 | 用途 |
|---|---|
| `vsc doctor` | 环境自检：ffmpeg、API Key、登录状态、磁盘（要 5 GB 以上）、模型配置，哪项不过会给出修复命令 |
| `vsc login` | 登录。不想在终端里输密码：`--username` 加 `--password-stdin`，或环境变量 `NJUPT_USERNAME` / `NJUPT_PASSWORD`；`--status` 只查会话；`--cookie` 可以导入浏览器里的登录态 |
| `vsc semesters` | 打印校历，看 `--semester` 能填什么（`current` 或 `2026-2027-1`） |
| `vsc list` | 录播列表。`--semester`、`--title`、`--start`、`--end`、`--limit`、`--order` |
| `vsc fetch <编号>` | 只下载。默认屏幕流带声音就不下摄像机流；`--full-download` 全下，`--types VGA,Video1` 指定 |
| `vsc prepare <编号>` | 只做本地处理：抽音频、切课件页。`--threshold`、`--max-slides`、`--max-gap` 调切页 |
| `vsc run` | 下载并整理，选项见上 |
| `vsc status <编号>` | 各步完成情况、错误、处理提示、生成了哪些文件、token 用量。不联网 |
| `vsc summary <编号>` | 笔记文件在哪；`--print` 把笔记原样写到 stdout |

### For agent

加 `--json`（放在命令前后都行）后，stdout 只输出一个 JSON 文档，日志全部转到 stderr。退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 其他错误：环境自检没过、本地处理失败、缺配置 |
| `2` | 命令行用法错误。这时不保证输出 JSON，原因在 stderr |
| `3` | 没登录或会话过期 |
| `4` | 模型服务、智慧课堂接口或网络出错 |
| `5` | 部分完成：有步骤失败了，其余已存盘，重跑同一条命令续上 |
| `6` | 没匹配到：筛选条件没有结果，或 `summary` 的那节课还没有笔记 |

其余说明在 `skills/very-smart-class/SKILL.md`，直接读取对照操作。

## 工作原理

处理一节课分八步。前两步不调用模型，中间几步各调用一种模型，上一步的结果是下一步的输入：

```
vsc run <编号>
  fetch       下载录播：屏幕流 VGA.mp4（自带声音）；需要时再下摄像机流 Video1.mp4
  prepare     本地处理：抽出音频；从画面里切出课件页（画面突变时切一页，去重，长时间不变补一页）
  transcribe  语音模型把老师说的话逐句转成文字，带起止时间
  vision      视觉模型描述每一页课件：标题、要点、字是否清晰
  summarize   文本模型按时间段写笔记，再汇总成全课笔记（转写模式）
  omni        多模态模型看缩小后的整段录像（每 5 秒一帧）并听声音（视频模式）
  fuse        文本模型把两种模式合并
  render      写出 summary.md
```

- **老师自带电脑投屏时**，录制的教室电脑桌面上只剩无用信息。程序先抽最多 8 张画面让视觉模型判断有没有授课内容，至少 2 张才算可用；否则改看摄像机画面，自动裁出投影幕区域再识别。判断的是「画面上是不是讲课用的材料」，和具体课程无关。
- **单步失败不污染整节课**。失败的步骤和原因会写进笔记末尾的「处理提示」，其余步骤照常。语音转写失败时仍会出一份只有画面内容的笔记，问题解决后重跑同一条命令就会补上。下载、本地处理或写笔记这一步本身失败时，这一节才没有笔记，同样重跑即可。
- **模型仅支持阿里云百炼**。其他供应商接口暂未实现，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 输出文件

```
data/<课程名>/<编号>/
├── summary.md            笔记。分成多段录制时，这里是各段笔记依次拼接
├── state.json            进度：每一步是否完成、错误、处理提示、token 用量
├── meta.json 等          录播的元信息：各路流的地址、开始时间、有没有声音
└── Seg1/                 每一段录制的文件；只有一段时没有这一层
    ├── VGA.mp4           屏幕流录像；Video1.mp4 是摄像机画面。--prune-video 会删掉它们
    ├── audio.wav         抽出来的音频，16 kHz 单声道
    ├── transcript.json   每句话的起止时间（毫秒）和文字
    ├── slides/           课件页截图，以及视觉模型对每一页的描述
    ├── c_result.json     转写模式的笔记
    ├── a_result_*.json   视频模式的笔记
    ├── final.json        两种模式合并后的结果，summary.md 由它写出
    └── summary.md        这一段的笔记
```

其余文件（`chunks.json`、`*_640.mp4`、`transcription_raw.json` 等）是各步骤的中间结果和留档。数据目录默认在项目的 `data/`，可以用 `VSC_DATA_DIR` 改。

## 配置

所有配置都可以放环境变量，或者写进项目根目录的 `.env`（已有的环境变量优先）。

| 变量 | 必填 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 是 | 阿里云百炼 API Key |
| `NJUPT_USERNAME` / `NJUPT_PASSWORD` | 否 | 非交互式注入账号密码 |
| `VSC_MODEL_ASR`、`VSC_MODEL_VISION`、`VSC_MODEL_TEXT`、`VSC_MODEL_OMNI` | 否 | 换模型，分别对应语音转写、看课件页、写笔记和合并、看整段视频。命令行 `--model-*` 优先级更高 |
| `VSC_DATA_DIR` | 否 | 数据目录，默认 `./data` |

默认模型（定义在 `src/njupt_very_smart_class/config.py`）：语音转写 `qwen-audio-3.1-asr-flash-filetrans`，课件页描述和写笔记 `qwen3.7-plus`，整段视频 `qwen3.8-omni-flash`。

## 费用和时间（仅供参考）

一节 45 分钟的课，全流程约 10 分钟、¥0.64；加 `--no-a-path` 约 5 分钟、¥0.37。下表按 5 节实测的 token 用量折算，单价取阿里云百炼华北 2 标价（查询于2026-11-25）。

| 步骤 | 均值 | 范围 | 说明 |
|---|---|---|---|
| 语音转写 | ¥0.047 | 0.044–0.049 | |
| 识别课件页 | ¥0.11 | 0.06–0.14 | 每页约 ¥0.0037，一节课 16–38 页，老师翻页越多越贵 |
| 写笔记（转写模式） | ¥0.21 | 0.19–0.25 | 模型开了思考（thinking） |
| 看整段视频（视频模式） | ¥0.18 | 0.165–0.207 | 一节课约 166K 视频 + 19.7K 音频 token |
| 合并两种模式 | ¥0.09 | 0.08–0.10 | |
| **合计** | **≈ ¥0.64** | 0.57–0.70 | `--no-a-path` ≈ ¥0.37 |

另外要下载约 170 MB（只下屏幕流）；需要摄像机画面时再加约 1.1 GB。

几种情况会让费用变化：

- **老师自带电脑投屏的课**：改看摄像机画面后，课件要每 30 秒取一帧来识别，一节 94 帧，识别这一项 ¥0.35，整节约 ¥0.86。
- **静音或空教室的录像**：语音转写在本地判断是否无声，无模型开销；但视频模式会把整段录像送给模型，加上合并约 ¥0.17。可使用 `--no-a-path` 避免。

## 已知限制

- 需要访问 `i.njupt.edu.cn` 和 `njupt.smartclass.cn`。
- 模型能力限制。「待人工复核」是两种模式对不上或模型拿不准的地方，「处理提示」说明这份笔记哪里可能不完整，复习时先看这两节。
- 分成多段录制的课，各段分别处理，时间戳不跨段对齐。
- 板书课（屏幕上没有内容、老师写粉笔字）：会改看摄像机画面并裁出黑板区域，但这条路径暂未真实验证。
- 超过 2 小时的录像不会自动切段。
- 录播的屏幕流和摄像机流开始时间可能不一样。程序会按两者的开始时间差校正时间戳；两者时长相差超过 5 秒时会在「处理提示」里标出，时间轴可能有偏差。
- 媒体文件已被平台删除的旧录像（仅课程列表可见）会下载失败，请自行选择近期的录播。

## 隐私

- 密码只在登录请求里用一次，不落盘；本地只保存登录会话的 Cookie（`data/.session.json`）。
- 录像和生成的笔记都留在本地。除了登录智慧课堂，涉及上传的只有需要模型分析的内容：音频、课件页截图、缩小后的录像，以及模型输出的文本。这些文件里包含他人的声音和肖像，仅供个人复习使用，禁止公开分享。

## 参与开发

开发环境、测试、代码约定等见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可与致谢

AGPL-3.0-or-later（见 [LICENSE](LICENSE)）。`src/njupt_very_smart_class/vendor/` 下两个文件来自
[ArcticLampyrid/njupt_smartclass_downloader](https://github.com/ArcticLampyrid/njupt_smartclass_downloader)（AGPL-3.0-or-later，commit `17d4779`），保留原始许可头。

本项目与南京邮电大学、翰博尔、阿里云均无关联；平台与接口随时可能变化。
