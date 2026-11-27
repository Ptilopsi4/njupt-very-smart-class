# SPDX-License-Identifier: AGPL-3.0-or-later
import json

from PIL import Image

from njupt_very_smart_class.media import compress_audio, extract_audio, has_audio, probe, same_duration
from njupt_very_smart_class.slides import (
    Slide,
    dhash,
    extract_slides,
    hamming,
    is_blank,
    mean_rgb,
    select_frames,
)


def test_probe_and_audio(synthetic_video):
    info = probe(synthetic_video)
    assert has_audio(info)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert abs(float(info["format"]["duration"]) - 9.0) < 0.5


def test_extract_audio_wav_16k_mono(synthetic_video, tmp_path):
    import wave

    wav = extract_audio(synthetic_video, tmp_path / "audio.wav")
    with wave.open(str(wav)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        assert abs(w.getnframes() / 16000 - 9.0) < 0.3
    assert extract_audio(synthetic_video, wav) == wav  # 幂等


def test_compress_audio_flac_for_upload(synthetic_video, tmp_path):
    wav = extract_audio(synthetic_video, tmp_path / "audio.wav")
    flac = compress_audio(wav, tmp_path / "audio.flac")
    audio = next(s for s in probe(flac)["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == "flac" and int(audio["sample_rate"]) == 16000 and audio["channels"] == 1
    assert same_duration(flac, wav) and flac.stat().st_size < wav.stat().st_size
    assert compress_audio(wav, flac) == flac  # 已有且等长即复用，不重新编码


def test_dhash_sensitivity():
    # linear_gradient 是上下渐变，转成左右渐变后镜像才会翻转所有比较位
    grad = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90)
    assert hamming(dhash(grad), dhash(grad.copy())) == 0
    assert hamming(dhash(grad), dhash(grad.transpose(Image.Transpose.FLIP_LEFT_RIGHT))) == 64


def test_mean_rgb_distinguishes_flat_colors():
    red = Image.new("RGB", (64, 64), (255, 0, 0))
    blue = Image.new("RGB", (64, 64), (0, 0, 255))
    assert dhash(red) == dhash(blue)  # dHash 对纯色失明
    assert mean_rgb(red) != mean_rgb(blue)


def _flat(tmp_path, name, color, delta=80):
    """带纹理的色块：左半 base、右半 base+delta。

    有起伏所以不会被当成空页；不同颜色 dHash 相同但色差大，仍能判为不同帧。
    """
    path = tmp_path / f"{name}.png"
    im = Image.new("RGB", (64, 64), color)
    lighter = tuple(min(255, c + delta) for c in color)
    im.paste(lighter, (32, 0, 64, 64))
    im.save(path)
    return path


def test_extract_slides_finds_scene_changes(synthetic_video, tmp_path):
    out = tmp_path / "slides"
    slides = extract_slides(synthetic_video, out, threshold=0.2)
    times = [s.time for s in slides]
    assert len(slides) == 3, times  # 0 s 红 / 3 s testsrc / 6 s 蓝，两次硬切
    assert abs(times[0]) < 0.2 and abs(times[1] - 3) < 0.3 and abs(times[2] - 6) < 0.3
    for s in slides:
        assert (out / s.file).exists()
    manifest = json.loads((out / "slides.json").read_text(encoding="utf-8"))
    assert manifest[0]["file"] == "slide_001.jpg" and manifest[0]["index"] == 1
    assert extract_slides(synthetic_video, out, threshold=0.2) == slides  # 同参数：幂等读回
    assert not (out / "raw").exists()


def test_extract_slides_recuts_when_params_change(synthetic_video, tmp_path):
    out = tmp_path / "slides"
    three = extract_slides(synthetic_video, out)
    assert len(three) == 3 and json.loads((out / "params.json").read_text(encoding="utf-8"))["max_slides"] == 150
    one = extract_slides(synthetic_video, out, max_slides=1)  # 参数变了：重切，旧截图清掉
    assert len(one) == 1 and not (out / "slide_002.jpg").exists()
    assert extract_slides(synthetic_video, out, max_slides=1) == one  # 同参数：读回
    assert len(extract_slides(synthetic_video, out)) == 3  # 改回默认也重切


def test_extract_slides_legacy_manifest_counts_as_defaults(synthetic_video, tmp_path):
    out = tmp_path / "slides"
    slides = extract_slides(synthetic_video, out)
    (out / "params.json").unlink()  # 旧版没记参数
    before = (out / "slide_001.jpg").stat().st_mtime_ns
    assert extract_slides(synthetic_video, out) == slides and (out / "params.json").exists()
    assert (out / "slide_001.jpg").stat().st_mtime_ns == before  # 按默认值看待，没有重切


def test_extract_slides_fills_long_gaps(synthetic_video, tmp_path):
    # 9 s 视频，硬切在第 3/6 s；max_gap=1.5 时在每段中点补帧：1.5 / 4.5 / 7.5
    slides = extract_slides(synthetic_video, tmp_path / "slides", threshold=0.2, max_gap=1.5)
    times = [round(s.time, 1) for s in slides]
    assert times == [0.0, 1.5, 3.0, 4.5, 6.0, 7.5], times
    for s in slides:
        assert (tmp_path / "slides" / s.file).exists()


def test_select_frames_rules(tmp_path):
    black = _flat(tmp_path, "black", (20, 20, 20))
    white = _flat(tmp_path, "white", (200, 200, 200))
    red = _flat(tmp_path, "red", (200, 0, 0))
    green = _flat(tmp_path, "green", (0, 255, 0))
    blue = _flat(tmp_path, "blue", (0, 0, 255))
    black2 = _flat(tmp_path, "black2", (21, 21, 21))  # 与 black 相似

    # 0.5 s 内：过渡帧丢弃；10 s 内且相似：丢弃
    frames = [(0.0, black), (0.5, red), (10.0, black2), (20.0, red)]
    assert [t for t, _ in select_frames(frames)] == [0.0, 20.0]
    # 静默 >= 30 s 后的相似帧：保留
    frames = [(0.0, black), (100.0, black2)]
    assert [t for t, _ in select_frames(frames)] == [0.0, 100.0]
    # 30 s 内但不相似：保留
    frames = [(0.0, black), (10.0, red), (20.0, green), (30.0, blue)]
    assert [t for t, _ in select_frames(frames)] == [0.0, 10.0, 20.0, 30.0]
    # max_slides 等距抽样
    frames = [(0.0, black), (10.0, red), (20.0, green), (30.0, blue)]
    assert [t for t, _ in select_frames(frames, max_slides=2)] == [0.0, 20.0]
    assert isinstance(Slide(1, 0.0, "x.jpg"), Slide)
    assert white.exists()


def test_is_blank():
    assert is_blank(Image.new("RGB", (64, 64), (0, 0, 0)))
    assert is_blank(Image.new("RGB", (64, 64), (8, 8, 8)))
    assert is_blank(Image.new("RGB", (64, 64), (255, 255, 255)))  # 纯白空页
    loading = Image.new("RGB", (64, 64), (0, 0, 0))
    loading.putpixel((32, 32), (200, 200, 200))
    assert is_blank(loading)  # 黑屏加载圈
    assert not is_blank(Image.linear_gradient("L").resize((64, 64)))
    terminal = Image.new("RGB", (64, 64), (30, 30, 30))
    for y in range(2, 64, 6):  # 长短不一的"文本行"，模拟真实终端的密度起伏
        for x in range(2, 2 + (y * 7) % 56):
            terminal.putpixel((x, y), (220, 220, 220))
    assert not is_blank(terminal)  # 深色终端


def test_select_frames_drops_blank(tmp_path):
    blank = tmp_path / "blank.png"
    Image.new("RGB", (64, 64), (0, 0, 0)).save(blank)
    red = _flat(tmp_path, "r", (200, 0, 0))
    assert select_frames([(0.0, blank), (10.0, red)]) == [(10.0, red)]


def test_is_blank_ignores_taskbar():
    # 黑屏 + 底部 10% 高亮任务栏 + 顶部标题栏：裁掉后才判为空页
    screen = Image.new("RGB", (1024, 768), (5, 5, 5))
    screen.paste(Image.new("RGB", (1024, 77), (180, 180, 180)), (0, 691))
    screen.paste(Image.new("RGB", (1024, 23), (120, 120, 120)), (0, 0))
    assert is_blank(screen)
